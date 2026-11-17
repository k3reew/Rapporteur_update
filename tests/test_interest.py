"""Сводка только для докладов, интересных отделам; остальным — карточка."""

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient

from rapporteur import interest
from rapporteur.llm.client import ScriptedClient

from .fictional_talk import SLIDES


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, ScriptedClient]]:
    from rapporteur.app import create_app

    scripted = ScriptedClient([])
    with TestClient(create_app(tmp_path, client=lambda _: scripted, process=False)) as client:
        yield client, scripted


def bound_talk(client: TestClient) -> tuple[str, str]:
    conference = client.post(
        "/api/v1/conferences", json={"title": "Пример", "start_date": "2026-11-12"}
    ).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    uploaded = client.post(
        base + "/materials",
        params={"path": "01_Доклад/речь.txt", "purpose": "document"},
        content="\n".join(SLIDES).encode(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert uploaded.status_code == 201, uploaded.text
    talk_id = client.get(base + "/materials").json()["talks"][0]["id"]
    saved = client.put(
        base + "/materials/bindings",
        json={"bindings": [{"source_file_id": uploaded.json()["id"], "talk_id": talk_id}]},
    )
    assert saved.status_code == 200, saved.text
    return conference, talk_id


def progress(client: TestClient, conference: str) -> dict[str, object]:
    talks: list[dict[str, object]] = client.get(
        f"/api/v1/conferences/{conference}/progress"
    ).json()["talks"]
    return talks[0]


def test_card_only_talk_skips_the_model_and_enters_the_report(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, scripted = web
    conference, talk_id = bound_talk(client)
    card = {
        "title": "Кэширование запросов",
        "speakers": [{"name": "Иванов И. И.", "affiliation": "Институт примера"}],
        "contacts": ["ivanov@example.org"],
    }
    assert client.put(f"/api/v1/talks/{talk_id}", json=card).status_code == 204
    assert client.put(f"/api/v1/talks/{talk_id}/scope", json={"scope": "card"}).status_code == 204
    assert client.post(f"/api/v1/conferences/{conference}/processing").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]

    talk = progress(client, conference)
    # Модель не вызывалась: у ScriptedClient нет ответов, иначе обработка упала бы.
    assert not scripted.requests
    assert (talk["job"], talk["status"], talk["card_only"], talk["scope"]) == (
        "done",
        "processed",
        True,
        "card",
    )
    assert talk["summary_number"] is None

    # Карточка не требует подтверждения непроверенных сводок.
    built = client.post(f"/api/v1/conferences/{conference}/reports", json={})
    assert built.status_code == 201, built.text
    version = built.json()
    assert version["input"]["talks"][0]["card_only"] is True
    assert version["input"]["talks"][0]["summary"] is None
    docx = client.get(f"/api/v1/report-versions/{version['id']}/files/docx").content
    text = [p.text for p in Document(io.BytesIO(docx)).paragraphs]
    start = text.index("1. «Кэширование запросов»")
    assert text[start + 1 : start + 3] == [
        "Автор: Иванов И. И. (Институт примера)",
        "Контакты: ivanov@example.org",
    ]
    assert len(text) == start + 3


def test_scope_change_requeues_processed_talk(web: tuple[TestClient, ScriptedClient]) -> None:
    client, _ = web
    conference, talk_id = bound_talk(client)
    client.put(f"/api/v1/talks/{talk_id}/scope", json={"scope": "card"})
    client.post(f"/api/v1/conferences/{conference}/processing")
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    assert progress(client, conference)["job"] == "done"

    assert client.put(f"/api/v1/talks/{talk_id}/scope", json={"scope": "summary"}).status_code == (
        204
    )
    assert progress(client, conference)["job"] == "queued"
    assert client.put(f"/api/v1/talks/{talk_id}/scope", json={"scope": "x"}).status_code == 422
    assert client.put("/api/v1/talks/missing/scope", json={"scope": "card"}).status_code == 404


@pytest.mark.parametrize(
    ("scope", "decision", "expected"),
    [
        ("auto", None, True),
        ("auto", True, True),
        ("auto", False, False),
        ("summary", False, True),
        ("card", True, False),
    ],
)
def test_operator_choice_overrides_classification(
    web: tuple[TestClient, ScriptedClient],
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    decision: bool | None,
    expected: bool,
) -> None:
    from .fictional_talk import pack

    client, _ = web
    _, talk_id = bound_talk(client)
    client.put(f"/api/v1/talks/{talk_id}/scope", json={"scope": scope})
    monkeypatch.setattr(interest, "classify", lambda conn, talk, source: decision)
    with client.app.state.database.read() as conn:  # type: ignore[attr-defined]
        assert interest.needs_summary(conn, talk_id, pack()) is expected
