"""Карточка доклада: тема, авторы, организации и контакты."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapporteur import db
from rapporteur.app import create_app
from rapporteur.llm.client import ScriptedClient


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, ScriptedClient]]:
    scripted = ScriptedClient([])
    with TestClient(create_app(tmp_path, client=lambda _: scripted, process=False)) as client:
        yield client, scripted


def _talk(client: TestClient) -> tuple[str, str]:
    conference = client.post("/api/v1/conferences", json={"title": "Карточки"}).json()["id"]
    review = client.post(
        f"/api/v1/conferences/{conference}/talks", json={"title": "Кэширование"}
    ).json()
    return conference, review["talks"][0]["id"]


def test_card_fields_are_saved_and_kept_by_older_clients(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, _ = web
    _, talk_id = _talk(client)
    card = {
        "title": "Кэширование",
        "speakers": [
            {
                "name": "Иванов И. И.",
                "affiliation": "МГТУ им. Н. Э. Баумана",
                "contacts": ["ivanov@example.org"],
            }
        ],
        "organizations": ["ИСП РАН"],
        "contacts": ["+7 495 000-00-00"],
    }
    assert client.put(f"/api/v1/talks/{talk_id}", json=card).status_code == 204
    saved = client.get(f"/api/v1/talks/{talk_id}").json()
    assert saved["speakers"][0]["contacts"] == ["ivanov@example.org"]
    assert saved["speakers"][0]["affiliation"] == "МГТУ им. Н. Э. Баумана"
    assert saved["organizations"] == ["ИСП РАН"]
    assert saved["contacts"] == ["+7 495 000-00-00"]

    # Правка только темы и авторов (как в прежнем интерфейсе) не стирает остальное.
    rename = {"title": "Кэширование данных", "speakers": saved["speakers"]}
    assert client.put(f"/api/v1/talks/{talk_id}", json=rename).status_code == 204
    renamed = client.get(f"/api/v1/talks/{talk_id}").json()
    assert renamed["title"] == "Кэширование данных"
    assert renamed["organizations"] == ["ИСП РАН"]
    assert renamed["contacts"] == ["+7 495 000-00-00"]

    cleared = rename | {"organizations": [], "contacts": []}
    assert client.put(f"/api/v1/talks/{talk_id}", json=cleared).status_code == 204
    assert client.get(f"/api/v1/talks/{talk_id}").json()["organizations"] == []
    assert client.put(f"/api/v1/talks/{talk_id}", json=rename | {"contacts": [""]}).status_code == (
        422
    )


def test_confirmed_proposal_writes_card(web: tuple[TestClient, ScriptedClient]) -> None:
    client, _ = web
    conference = client.post("/api/v1/conferences", json={"title": "Без папок"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    uploaded = client.post(
        base + "/materials",
        params={"path": "речь.txt", "purpose": "document"},
        headers={"Content-Type": "application/octet-stream"},
        content="Тема доклада: кэширование.".encode(),
    )
    assert uploaded.status_code == 201, uploaded.text
    with client.app.state.database.tx() as conn:  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO preparations(conference_id,version,job) VALUES (?,1,'done')", (conference,)
        )
    proposal = {
        "version": 1,
        "talks": [
            {
                "title": "Кэширование",
                "speakers": [{"name": "Иванов И. И.", "contacts": ["ivanov@example.org"]}],
                "organizations": ["ИСП РАН"],
                "contacts": ["https://example.org"],
                "links": [{"file_id": uploaded.json()["id"]}],
            }
        ],
    }
    response = client.put(base + "/preparation", json=proposal)
    assert response.status_code == 200, response.text
    talk_id = response.json()["talks"][0]["talk_id"]
    card = client.get(f"/api/v1/talks/{talk_id}").json()
    assert card["speakers"][0]["contacts"] == ["ivanov@example.org"]
    assert card["organizations"] == ["ИСП РАН"]
    assert card["contacts"] == ["https://example.org"]


def test_v1_database_gets_card_columns_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "rapporteur.db"
    database = db.Database(path)
    conference, section, talk_id = db.new_id(), db.new_id(), db.new_id()
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO conferences(id, title, created_at) VALUES (?, 'Старая', ?)",
            (conference, db.now()),
        )
        conn.execute(
            "INSERT INTO sections(id, conference_id, title, position) VALUES (?, ?, 'С', 1)",
            (section, conference),
        )
        conn.execute(
            "INSERT INTO talks(id, section_id, position, title, speakers) "
            "VALUES (?, ?, 1, 'Доклад', '[{\"name\": \"Иванов И. И.\"}]')",
            (talk_id, section),
        )
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE talks DROP COLUMN organizations")
        conn.execute("ALTER TABLE talks DROP COLUMN contacts")
        conn.execute("PRAGMA user_version = 1")

    upgraded = db.Database(path)
    assert (tmp_path / "rapporteur.db.before-v2.bak").is_file()
    with upgraded.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        talk = db.talk(db.conference_talks(conn, conference)[0])
    assert talk.organizations == [] and talk.contacts == []
    assert talk.speakers[0].contacts == []
