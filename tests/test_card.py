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


def test_contacts_are_found_without_model() -> None:
    from rapporteur.ingest.card import find_contacts

    text = (
        "E-mail: Ivanov@BMSTU.ru, тел. +7 (495) 123-45-67, 8 800 555-35-35; "
        "сайт https://example.org/lab. ORCID 0000-0002-1825-0097, t.me/lab_news. "
        "В 2026 году заслушано 8 докладов по 15 темам."
    )
    assert find_contacts(text) == [
        "ivanov@bmstu.ru",
        "+7 (495) 123-45-67",
        "8 800 555-35-35",
        "https://example.org/lab",
        "ORCID 0000-0002-1825-0097",
        "t.me/lab_news",
    ]


def test_card_hints_attribute_by_line_email_and_single_author() -> None:
    from rapporteur.ingest.card import card_hints

    title = "Кэширование в распределённых системах"
    hints = card_hints(
        [
            title,
            "Иванов И. И.¹, Петров П. П.²",
            "¹МГТУ им. Н. Э. Баумана",
            "²ИСП РАН",
            "ivanov@bmstu.ru",
            "+7 495 000-00-00",
        ],
        ["Иванов И. И.", "Петров П. П."],
        title,
    )
    assert hints.organizations == ["МГТУ им. Н. Э. Баумана", "ИСП РАН"]
    assert hints.affiliations == {}
    assert hints.speaker_contacts == {"Иванов И. И.": ["ivanov@bmstu.ru"]}
    assert hints.contacts == ["+7 495 000-00-00"]

    same_line = card_hints(
        ["Петров П. П., ИСП РАН, petrov@ispras.ru", "Иванов И. И., МГТУ им. Н. Э. Баумана"],
        ["Иванов И. И.", "Петров П. П."],
    )
    assert same_line.affiliations == {
        "Петров П. П.": "ИСП РАН",
        "Иванов И. И.": "МГТУ им. Н. Э. Баумана",
    }
    assert same_line.speaker_contacts == {"Петров П. П.": ["petrov@ispras.ru"]}
    assert same_line.organizations == []

    single = card_hints(
        [
            "Цифровой университет",
            "Сидоров Алексей Петрович",
            "АО «НПО Технология»",
            "Тел.: +7 (495) 123-45-67",
            "Спасибо за внимание!",
            "www.example.org",
        ],
        ["Сидоров Алексей Петрович"],
        "Цифровой университет",
    )
    assert single.affiliations == {"Сидоров Алексей Петрович": "АО «НПО Технология»"}
    assert single.speaker_contacts == {
        "Сидоров Алексей Петрович": ["+7 (495) 123-45-67", "www.example.org"]
    }
    assert single.organizations == [] and single.contacts == []


def test_email_matches_common_transliterations() -> None:
    from rapporteur.ingest.card import card_hints

    hints = card_hints(
        ["habarov.ae@example.org", "tsoy@example.org", "unknown@example.org"],
        ["Хабаров А. Е.", "Цой В. Р."],
    )
    assert hints.speaker_contacts == {
        "Хабаров А. Е.": ["habarov.ae@example.org"],
        "Цой В. Р.": ["tsoy@example.org"],
    }
    assert hints.contacts == ["unknown@example.org"]


def test_preparation_proposes_card_from_first_page(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    import json

    client, scripted = web
    conference = client.post("/api/v1/conferences", json={"title": "Карточки"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    text = "\n".join(
        [
            "Кэширование в распределённых системах",
            "Иванов И. И.",
            "МГТУ им. Н. Э. Баумана",
            "ivanov@bmstu.ru",
            "Доклад описывает кэширование ответов в центре обработки данных.",
        ]
    )
    uploaded = client.post(
        base + "/materials",
        params={"path": "речь.txt", "purpose": "document"},
        headers={"Content-Type": "application/octet-stream"},
        content=text.encode(),
    )
    assert uploaded.status_code == 201, uploaded.text
    scripted.responses.append(
        json.dumps(
            {
                "title": "Кэширование в распределённых системах",
                "speakers": ["Иванов И. И."],
                "talk_index": None,
                "confidence": 1,
                "reason": "Титульная страница",
            }
        )
    )
    assert client.post(base + "/preparation").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    talk = client.get(base + "/preparation").json()["talks"][0]
    assert talk["speakers"] == [
        {
            "name": "Иванов И. И.",
            "affiliation": "МГТУ им. Н. Э. Баумана",
            "role": "speaker",
            "contacts": ["ivanov@bmstu.ru"],
        }
    ]
    assert talk["organizations"] == [] and talk["contacts"] == []


def test_card_keeps_registry_values_and_fills_only_gaps() -> None:
    from rapporteur.contracts import Speaker
    from rapporteur.contracts.enums import TalkStatus
    from rapporteur.materials import ReviewTalk
    from rapporteur.preparation import Link, ProposedTalk, with_card

    known = ReviewTalk(
        id="t",
        section_id="s",
        section="Секция",
        order=1,
        title="Кэширование",
        status=TalkStatus.MATCHED,
        speakers=[Speaker(name="Иванов И. И.", affiliation="ИСП РАН")],
        organizations=["Исправлено оператором"],
    )
    proposal = ProposedTalk(
        talk_id="t",
        title="Кэширование",
        speakers=[Speaker(name="Иванов И. И."), Speaker(name="Петров П. П.")],
        links=[Link(file_id="f")],
    )
    result = with_card(
        proposal,
        known,
        ["Иванов И. И., МГТУ", "Петров П. П., АО «Пример»", "petrov@example.org", "+7 495 1112233"],
    )
    assert [(s.name, s.affiliation, s.contacts) for s in result.speakers] == [
        ("Иванов И. И.", "ИСП РАН", []),
        ("Петров П. П.", "АО «Пример»", ["petrov@example.org"]),
    ]
    assert result.organizations == ["Исправлено оператором"]
    assert result.contacts == ["+7 495 1112233"]
