import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from rapporteur import db, materials, model_choice, processing, screening
from rapporteur.app import create_app
from rapporteur.llm.client import ChatRequest, ChatResponse, ScriptedClient

from .fictional_talk import (
    PROFILE,
    SLIDES,
    facts_answer,
    pack,
    summary_answer,
    supported_summary_answer,
)
from .workflow_support import DEPARTMENT, WorkflowClient, seed_regulation


def test_start_analysis_reuses_job_and_sends_clear_proposal_to_screening(tmp_path: Path) -> None:
    scripted = ScriptedClient(
        [
            json.dumps(
                {
                    "title": "Кэширование",
                    "speakers": ["Иванов И. И."],
                    "talk_index": None,
                    "confidence": 1,
                    "reason": "Текст выступления",
                }
            ),
        ]
    )
    app = create_app(tmp_path, client=lambda _: scripted, process=False)
    seed_regulation(app.state.database)
    with TestClient(app) as client:
        conference = client.post("/api/v1/conferences", json={"title": "Пример"}).json()["id"]
        base = f"/api/v1/conferences/{conference}"
        upload = client.post(
            base + "/materials",
            params={"path": "речь.txt", "purpose": "document"},
            headers={"Content-Type": "application/octet-stream"},
            content="Тема доклада: кэширование. Иванов И. И.".encode(),
        )
        assert upload.status_code == 201, upload.text
        started = client.post(base + "/preparation", json={"auto_start": True})
        duplicate = client.post(base + "/preparation", json={"auto_start": True})
        assert started.status_code == duplicate.status_code == 202
        assert started.json() == duplicate.json()
        assert client.get("/api/v1/conferences").json()[0]["resume_step"] == "talks"
        assert app.state.processor.run_once()
        ready = client.get(base + "/preparation").json()
        assert ready["job"] == "confirmed", ready
        progress = client.get(base + "/progress").json()
        assert progress["processing_started"] and progress["auto_start"]
        assert progress["talks"][0]["screening"]["state"] == "queued"
        assert progress["talks"][0]["speakers"] == ["Иванов И. И."]
        assert client.get("/api/v1/conferences").json()[0]["resume_step"] == "review"
        upload = client.post(
            base + "/materials",
            params={"path": "ещё.txt", "purpose": "document"},
            headers={"Content-Type": "application/octet-stream"},
            content="Ещё один доклад".encode(),
        )
        assert upload.status_code == 201, upload.text
        assert client.get(base + "/preparation").json()["job"] == "awaiting_start"
        assert client.get("/api/v1/conferences").json()[0]["resume_step"] == "materials"
        restarted = client.post(base + "/preparation", json={"auto_start": True})
        assert restarted.json()["job"] == "queued"


def test_positive_draft_is_reused_but_cannot_be_published_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_choice, "load_profile", lambda: PROFILE)
    facts = re.sub(r'"S\d+"', '"D1"', facts_answer(listener_remarks=[]))
    scripted = WorkflowClient(
        [
            json.dumps(
                {
                    "title": SLIDES[0].split("\n")[0],
                    "speakers": ["Иванов И. И."],
                    "talk_index": None,
                    "confidence": 1,
                    "reason": "Текст доклада",
                }
            ),
            facts,
            facts,
            summary_answer(),
            supported_summary_answer(),
        ]
    )
    app = create_app(tmp_path, client=lambda _: scripted, process=False)
    seed_regulation(app.state.database)
    with TestClient(app) as client:
        conference = client.post(
            "/api/v1/conferences",
            json={
                "title": "Пример",
                "start_date": "2026-11-12",
            },
        ).json()["id"]
        base = f"/api/v1/conferences/{conference}"
        assert (
            client.post(
                base + "/materials",
                params={"path": "речь.txt", "purpose": "document"},
                headers={"Content-Type": "application/octet-stream"},
                content="\n".join(SLIDES).encode(),
            ).status_code
            == 201
        )
        assert client.post(base + "/preparation", json={"auto_start": True}).status_code == 202
        assert app.state.processor.run_once()
        assert app.state.processor.run_once()
        item = client.get(base + "/progress").json()["talks"][0]
        tid = item["id"]
        assert item["summary_number"] == 1 and item["job"] == "done", item
        assert not item["reportable"] and not item["screening"]["manual"]
        selected = {"talk_ids": [tid], "unreviewed_confirmed": True}
        assert client.post(base + "/reports", json=selected).status_code == 409
        accepted = client.put(
            f"/api/v1/talks/{tid}/screening",
            json={
                "version": item["screening"]["version"],
                "department_ids": [DEPARTMENT],
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert not app.state.processor.run_once()
        assert client.post(base + "/reports", json=selected).status_code == 409
        approved = client.post(f"/api/v1/talks/{tid}/summary/review", json={"version": 1})
        assert approved.status_code == 200, approved.text
        assert client.get(base + "/progress").json()["talks"][0]["reportable"]
        queued = client.post(
            base + "/report-jobs", json={**selected, "idempotency_key": "before-review-change"}
        )
        assert queued.status_code == 202, queued.text
        job = queued.json()["id"]
        declined = client.put(
            f"/api/v1/talks/{tid}/screening",
            json={
                "version": accepted.json()["version"],
                "department_ids": [],
            },
        )
        assert declined.status_code == 200, declined.text
        assert client.post(base + "/reports", json=selected).status_code == 409
        assert app.state.processor.run_once()
        assert client.get(f"/api/v1/report-jobs/{job}").json()["status"] == "failed"
        assert client.get(base + "/reports").json() == []
        assert client.post(f"/api/v1/report-jobs/{job}/retry").status_code == 409
        assert app.state.processor.run_once()
        result = client.get(base + "/progress").json()["talks"][0]
        assert result["reportable"] and result["card_only"]


def test_two_workers_can_screen_different_talks_once_each(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_choice, "load_profile", lambda: PROFILE)
    monkeypatch.setenv("RAPPORTEUR_INFERENCE_SLOTS", "2")
    rendezvous = Barrier(2, timeout=5)

    class ConcurrentClient(WorkflowClient):
        def chat(self, request: ChatRequest) -> ChatResponse:
            rendezvous.wait()
            return super().chat(request)

    app = create_app(tmp_path, client=lambda _: ConcurrentClient([]), process=False)
    database = app.state.database
    seed_regulation(database)
    cid = db.new_id()
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO conferences(id,title,created_at) VALUES(?,?,?)",
            (cid, "Два доклада", db.now()),
        )
        talks = [materials.add_talk(conn, cid, f"Доклад {i}", None) for i in range(2)]
        for talk in talks:
            screening.queue(conn, talk)
    processor: processing.Processor = app.state.processor
    monkeypatch.setattr(
        processor,
        "_screen_pack",
        lambda talk: pack().model_copy(
            update={"talk_id": UUID(talk)},
        ),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert processor.run_once(pool)
    with database.read() as conn:
        assert [screening.view(conn, t)["state"] for t in talks] == ["relevant", "relevant"]
        assert conn.execute("SELECT count(*) FROM talk_screenings").fetchone()[0] == 2
        assert conn.execute("SELECT sum(worker_active) FROM talks").fetchone()[0] == 0
