"""Durable report jobs freeze sources and publish one complete version."""

import io
from pathlib import Path
from typing import Any, cast

import pytest
from docx import Document
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from rapporteur.llm.client import ScriptedClient
from rapporteur.report.export import render_formats

from .test_multimodal import create_document_talk
from .test_multimodal import web as web


def submit(client: TestClient, conference: str, talk: str, key: str = "request-1") -> Response:
    return client.post(
        f"/api/v1/conferences/{conference}/report-jobs",
        json={"idempotency_key": key, "talk_ids": [talk], "unreviewed_confirmed": True},
    )


def test_report_job_survives_refresh_and_freezes_sources(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    response = submit(client, conference, talk)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued"
    assert client.get(f"/api/v1/conferences/{conference}/report-jobs").json()[0]["id"] == job["id"]
    with cast(FastAPI, client.app).state.database.tx() as conn:
        conn.execute("UPDATE talks SET title='Другое название' WHERE id=?", (talk,))
    assert cast(FastAPI, client.app).state.processor.run_once()
    completed = client.get(f"/api/v1/report-jobs/{job['id']}").json()
    assert completed["status"] == "done", completed
    report = client.get(f"/api/v1/conferences/{conference}/reports").json()[0]
    assert report["id"] == completed["report_version_id"]
    assert {item["format"] for item in report["files"]} == {"docx", "md", "pdf"}
    content = client.get(f"/api/v1/report-versions/{report['id']}/files/docx").content
    assert "Другое название" not in "\n".join(
        p.text for p in Document(io.BytesIO(content)).paragraphs
    )


def test_report_job_idempotency_rejects_a_changed_request(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    first = submit(client, conference, talk)
    assert first.status_code == 202
    assert submit(client, conference, talk).json()["id"] == first.json()["id"]
    changed = client.post(
        f"/api/v1/conferences/{conference}/report-jobs",
        json={"idempotency_key": "request-1", "talk_ids": [], "unreviewed_confirmed": True},
    )
    assert changed.status_code == 409
    cast(FastAPI, client.app).state.processor.run_once()
    cast(FastAPI, client.app).state.processor.run_once()
    assert len(client.get(f"/api/v1/conferences/{conference}/reports").json()) == 1


def test_failed_export_is_retryable_without_partial_report(
    web: tuple[TestClient, ScriptedClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur import report_jobs
    from rapporteur.report.export import ExportError

    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    job = submit(client, conference, talk).json()
    original = render_formats

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ExportError("PDF не собран: тестовый таймаут")

    monkeypatch.setattr(report_jobs, "render_formats", fail)
    cast(FastAPI, client.app).state.processor.run_once()
    failed = client.get(f"/api/v1/report-jobs/{job['id']}").json()
    assert failed["status"] == "failed"
    assert "PDF" in failed["error"]
    assert client.get(f"/api/v1/conferences/{conference}/reports").json() == []
    monkeypatch.setattr(report_jobs, "render_formats", original)
    assert client.post(f"/api/v1/report-jobs/{job['id']}/retry").status_code == 202
    cast(FastAPI, client.app).state.processor.run_once()
    assert client.get(f"/api/v1/report-jobs/{job['id']}").json()["status"] == "done"
    assert len(client.get(f"/api/v1/conferences/{conference}/reports").json()) == 1


def test_report_template_content_is_frozen(
    web: tuple[TestClient, ScriptedClient], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from rapporteur import report_jobs
    from rapporteur.settings import TEMPLATES_DIR

    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    shutil.copytree(TEMPLATES_DIR / "starter", tmp_path / "template")
    monkeypatch.setattr(report_jobs, "TEMPLATE_DIR", tmp_path / "template")
    job = submit(client, conference, talk).json()
    (tmp_path / "template/report.docx").write_bytes(b"changed template")
    cast(FastAPI, client.app).state.processor.run_once()
    assert client.get(f"/api/v1/report-jobs/{job['id']}").json()["status"] == "done"


def test_default_selection_omits_filtered_talks(web: tuple[TestClient, ScriptedClient]) -> None:
    from rapporteur import materials

    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    with cast(FastAPI, client.app).state.database.tx() as conn:
        materials.add_talk(conn, conference, "Пропущенный доклад", None)
    response = client.post(
        f"/api/v1/conferences/{conference}/report-jobs",
        json={"idempotency_key": "default-selection", "unreviewed_confirmed": True},
    )
    assert response.status_code == 202, response.text
    assert response.json()["source_versions"] == {talk: 1}


def test_request_identifier_is_returned_on_failure(web: tuple[TestClient, ScriptedClient]) -> None:
    client, _ = web
    response = client.get("/api/v1/report-jobs/missing", headers={"X-Request-ID": "operator-42"})
    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "operator-42"
    assert response.json()["request_id"] == "operator-42"


def test_running_report_waits_after_restart_and_continues_on_request(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    import time

    from rapporteur.processing import Processor

    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    state = cast(FastAPI, client.app).state
    job = submit(client, conference, talk).json()
    with state.database.tx() as conn:
        conn.execute("UPDATE report_jobs SET status='running',attempt=1 WHERE id=?", (job["id"],))
    worker = Processor(state.database, state.storage, lambda _: scripted)
    worker.start()
    try:
        # После перезапуска сборка приостановлена и сама не продолжается.
        assert not worker.run_once()
        paused = client.get(f"/api/v1/report-jobs/{job['id']}").json()
        assert (paused["status"], paused["step"]) == ("paused", "paused")
        assert client.post(f"/api/v1/report-jobs/{job['id']}/retry").status_code == 202
        worker.wake()
        for _ in range(300):
            value = client.get(f"/api/v1/report-jobs/{job['id']}").json()
            if value["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        assert value["status"] == "done", value
        assert value["attempt"] == 2
        assert len(client.get(f"/api/v1/conferences/{conference}/reports").json()) == 1
    finally:
        worker.stop()


def test_internal_error_returns_json_and_request_identifier(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    application = cast(FastAPI, web[0].app)

    @application.get("/broken-probe")
    def broken() -> None:
        raise RuntimeError("Ошибка тестового сервера")

    with TestClient(application, raise_server_exceptions=False) as client:
        result = client.get("/broken-probe", headers={"X-Request-ID": "internal-probe"})
    assert result.status_code == 500
    assert result.headers["X-Request-ID"] == "internal-probe"
    assert result.json()["request_id"] == "internal-probe"


def test_missing_report_template_is_actionable(
    web: tuple[TestClient, ScriptedClient], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from rapporteur import report_jobs

    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    monkeypatch.setattr(report_jobs, "TEMPLATE_DIR", tmp_path / "missing-template")
    with TestClient(cast(FastAPI, client.app), raise_server_exceptions=False) as safe:
        response = submit(safe, conference, talk)
    assert response.status_code == 500
    assert "бланк" in response.json()["message"]
    assert response.json()["request_id"]


def test_operator_review_draft_publish_and_export(
    web: tuple[TestClient, ScriptedClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from rapporteur import report_jobs

    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    state = cast(FastAPI, client.app).state
    with state.database.tx() as conn:
        conn.execute(
            "INSERT INTO preparations (conference_id,job,workflow,auto_start,proposal) "
            "VALUES (?,'confirmed','operator',1,?) "
            "ON CONFLICT(conference_id) DO UPDATE SET job='confirmed',workflow='operator',"
            "auto_start=1,proposal=excluded.proposal",
            (conference, json.dumps([{"talk_id": talk}])),
        )
        conn.execute(
            "UPDATE conferences SET start_date=NULL,end_date=NULL WHERE id=?", (conference,)
        )
        conn.execute("UPDATE talks SET status='reviewed' WHERE id=?", (talk,))
    base = f"/api/v1/conferences/{conference}"
    before = client.get(base + "/report-draft").json()
    assert before["revision"] == 0
    request = {"revision": 0, "fields": {"participation": "Делегация отдела"}}
    saved = client.put(base + "/report-draft", json=request)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert client.put(base + "/report-draft", json=request).status_code == 409

    assert client.post(base + "/report-jobs", json={"idempotency_key": "first"}).status_code == 409
    with state.database.tx() as conn:
        conn.execute(
            "UPDATE preparations SET screening_confirmed=1 WHERE conference_id=?", (conference,)
        )
    queued = client.post(
        base + "/report-jobs", json={"idempotency_key": "second", "draft_revision": 1}
    )
    assert queued.status_code == 202, queued.text

    original = render_formats

    def modified_while_rendering(*args: Any, **kwargs: Any) -> Any:
        rendered = original(*args, **kwargs)
        changed = client.put(
            base + "/report-draft",
            json={"revision": 1, "fields": {"participation": "Обновлённые данные"}},
        )
        assert changed.status_code == 200, changed.text
        return rendered

    monkeypatch.setattr(report_jobs, "render_formats", modified_while_rendering)
    assert state.processor.run_once()
    assert client.get(base + "/reports").json() == []
    failed = client.get(f"/api/v1/report-jobs/{queued.json()['id']}").json()
    assert failed["status"] == "failed"
    assert "Черновик справки изменился" in failed["error"]

    monkeypatch.setattr(report_jobs, "render_formats", original)
    replacement = client.post(
        base + "/report-jobs", json={"idempotency_key": "third", "draft_revision": 2}
    )
    assert replacement.status_code == 202, replacement.text
    assert state.processor.run_once()
    report = client.get(base + "/reports").json()[0]
    assert report["input"]["period_start"] is None
    assert {item["format"] for item in report["files"]} == {"docx", "md", "pdf"}
    assert not any("Рекомендации отделам" in warning for warning in report["warnings"])
    md = client.get(f"/api/v1/report-versions/{report['id']}/files/md")
    assert "Обновлённые данные" in md.text
