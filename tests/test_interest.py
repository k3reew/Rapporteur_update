"""Сводка для докладов, интересных отделам; остальным — карточка (interest.py)."""

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from fastapi.testclient import TestClient

from rapporteur import interest
from rapporteur.llm.client import ChatRequest, ChatResponse

from .fictional_talk import SLIDES
from .workflow_support import DEPARTMENT, WorkflowClient, seed_regulation


class Classifier(WorkflowClient):
    """Отбор отделов отвечает заданным решением; остальные ответы задаёт тест."""

    def __init__(self) -> None:
        super().__init__([])
        self.status = "not_relevant"

    def chat(self, request: ChatRequest) -> ChatResponse:
        schema: dict[str, Any] = request.schema or {}
        if schema.get("title") == "InterestAnswer" and self.status != "relevant":
            answer = {
                "interest": {"not_relevant": 10, "uncertain": 50}[self.status],
                "reason": "Вымышленное решение отбора",
                "evidence": [],
                "paragraphs": [],
            }
            return ChatResponse(json.dumps(answer, ensure_ascii=False), "", "stop", 100, 50, 0.0)
        return super().chat(request)


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, Classifier]]:
    from rapporteur.app import create_app

    scripted = Classifier()
    application = create_app(tmp_path, client=lambda _: scripted, process=False)
    seed_regulation(application.state.database)
    with TestClient(application) as client:
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


def run(client: TestClient) -> None:
    while client.app.state.processor.run_once():  # type: ignore[attr-defined]
        pass


def decide(client: TestClient, conference: str, talk_id: str, departments: list[str]) -> None:
    """Кнопка этапа «Проверка»: «Подтвердить интерес» выбранных отделов или «Только карточка»."""
    version = progress(client, conference)["screening"]["version"]  # type: ignore[index]
    decided = client.put(
        f"/api/v1/talks/{talk_id}/screening",
        json={"version": version, "department_ids": departments},
    )
    assert decided.status_code == 200, decided.text


def test_model_decision_waits_and_card_is_made_after_the_operator(
    web: tuple[TestClient, Classifier],
) -> None:
    client, scripted = web
    conference, talk_id = bound_talk(client)
    card = {
        "title": "Кэширование запросов",
        "speakers": [{"name": "Иванов И. И.", "affiliation": "Институт примера"}],
        "contacts": ["ivanov@example.org"],
    }
    assert client.put(f"/api/v1/talks/{talk_id}", json=card).status_code == 204
    assert client.post(f"/api/v1/conferences/{conference}/processing").status_code == 202
    run(client)

    # Модель решила «не интересен», но это лишь предложение: доклад ждёт оператора.
    talk = progress(client, conference)
    assert talk["screening"]["state"] == "not_relevant"  # type: ignore[index]
    assert talk["screening"]["manual"] is False  # type: ignore[index]
    assert (talk["job"], talk["step"], talk["card_only"], talk["reportable"]) == (
        None,
        "screening",
        False,
        False,
    )

    decide(client, conference, talk_id, [])
    run(client)
    talk = progress(client, conference)
    # Факты и сводка не запрашивались: ScriptedClient без ответов упал бы.
    assert not scripted.requests
    assert (talk["job"], talk["status"], talk["card_only"], talk["reportable"]) == (
        "done",
        "processed",
        True,
        True,
    )
    assert talk["summary_number"] is None

    # Фоновый выпуск без списка докладов берёт доклады с актуальным отбором, включая карточки;
    # карточка не требует подтверждения непроверенных сводок.
    job = client.post(
        f"/api/v1/conferences/{conference}/report-jobs", json={"idempotency_key": "card"}
    )
    assert job.status_code == 202, job.text
    run(client)
    done = client.get(f"/api/v1/report-jobs/{job.json()['id']}").json()
    assert done["status"] == "done", done["error"]
    version = client.get(f"/api/v1/conferences/{conference}/reports").json()[0]
    assert version["input"]["talks"][0]["card_only"] is True
    assert version["input"]["talks"][0]["summary"] is None
    # Рекомендации строятся по сводке: у карточки их нет, предупреждать не о чем.
    assert not [w for w in version["warnings"] if w.startswith("Рекомендации отделам")]
    docx = client.get(f"/api/v1/report-versions/{version['id']}/files/docx").content
    text = [p.text for p in Document(io.BytesIO(docx)).paragraphs]
    start = text.index("1. «Кэширование запросов»")
    assert text[start + 1 : start + 3] == [
        "Автор: Иванов И. И. (Институт примера)",
        "Контакты: ivanov@example.org",
    ]
    assert len(text) == start + 3


def test_undecided_talk_waits_for_the_operator(web: tuple[TestClient, Classifier]) -> None:
    client, scripted = web
    scripted.status = "uncertain"
    conference, talk_id = bound_talk(client)
    client.post(f"/api/v1/conferences/{conference}/processing")
    run(client)

    talk = progress(client, conference)
    assert (talk["job"], talk["step"], talk["card_only"], talk["reportable"]) == (
        None,
        "screening",
        False,
        False,
    )
    refused = client.post(
        f"/api/v1/conferences/{conference}/report-jobs",
        json={"idempotency_key": "wait", "talk_ids": [talk_id]},
    )
    assert refused.status_code == 409 and refused.json()["code"] == "screening_required"

    # «Только карточка»: пустой список отделов.
    decide(client, conference, talk_id, [])
    assert progress(client, conference)["job"] == "queued"
    run(client)
    talk = progress(client, conference)
    assert (talk["job"], talk["card_only"], talk["reportable"]) == ("done", True, True)
    assert not scripted.requests


def test_operator_interest_sends_card_talk_to_summary(web: tuple[TestClient, Classifier]) -> None:
    client, _ = web
    conference, talk_id = bound_talk(client)
    client.post(f"/api/v1/conferences/{conference}/processing")
    run(client)
    decide(client, conference, talk_id, [])
    run(client)
    talk = progress(client, conference)
    assert talk["card_only"] is True

    decide(client, conference, talk_id, [DEPARTMENT])
    talk = progress(client, conference)
    # До новой обработки карточка не соответствует отбору и в справку не выбирается.
    assert (talk["job"], talk["reportable"]) == ("queued", False)
    assert talk["screening"]["state"] == "relevant"  # type: ignore[index]


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("relevant", interest.Route.SUMMARY),
        ("not_relevant", interest.Route.CARD),
        ("uncertain", None),
        ("stale", None),
        ("queued", None),
        ("running", None),
        ("not_started", None),
    ],
)
def test_operator_decision_decides_summary_or_card(
    state: str, expected: interest.Route | None
) -> None:
    assert interest.decided({"state": state, "manual": True}) is expected
    # Решение модели без подтверждения оператора ничего не запускает.
    assert interest.decided({"state": state, "manual": False}) is None
