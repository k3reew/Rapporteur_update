"""Fictional pre-redesign installation: manual links, split audio and issued report."""

import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapporteur.app import create_app


def test_existing_installation_keeps_registry_sources_and_issued_reports(tmp_path: Path) -> None:
    with tarfile.open(Path(__file__).parent / "fixtures/legacy-installation.tar.gz") as archive:
        archive.extractall(tmp_path, filter="data")
    fixture = json.loads((tmp_path / "fixture.json").read_text())
    with TestClient(create_app(tmp_path, process=False)) as client:
        conferences = client.get("/api/v1/conferences").json()
        assert len(conferences) == 4
        base = "/api/v1/conferences/" + fixture["manual_conference"]
        review = client.get(base + "/materials").json()
        assert review["files"][0]["binding"]["confirmed"]
        assert review["files"][0]["binding"]["talk_id"] == fixture["manual_talk"]
        assert client.get(base + "/preparation").json()["job"] is None
        assert client.get("/api/v1/talks/" + fixture["manual_talk"] + "/summary").json()["summary"][
            "sentences"
        ]
        split = [
            client.get("/api/v1/conferences/" + item["id"] + "/preparation").json()
            for item in conferences
        ]
        assert any(len(state["talks"]) >= 2 for state in split)
        for report in fixture["reports"]:
            assert report["id"] in [item["id"] for item in client.get(base + "/reports").json()]
            for file in report["files"]:
                response = client.get(
                    "/api/v1/report-versions/" + report["id"] + "/files/" + file["format"]
                )
                assert response.status_code == 200
                assert hashlib.sha256(response.content).hexdigest() == file["sha256"]


def test_restart_pauses_unfinished_jobs_until_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Перенесённое или прерванное хранилище: после старта модель ничего не делает сама."""
    from rapporteur import db
    from rapporteur.llm.client import ScriptedClient
    from rapporteur.processing import Processor
    from rapporteur.storage import Storage

    with tarfile.open(Path(__file__).parent / "fixtures/legacy-installation.tar.gz") as archive:
        archive.extractall(tmp_path, filter="data")
    fixture = json.loads((tmp_path / "fixture.json").read_text())
    database = db.Database(tmp_path / "rapporteur.db")
    with database.tx() as conn:
        conn.execute(
            "UPDATE talks SET job='running',step='facts' WHERE id=?", (fixture["manual_talk"],)
        )
        conn.execute("UPDATE preparations SET job='queued'")
        conn.execute("UPDATE recommendation_jobs SET job='running'")
    processor = Processor(database, Storage(tmp_path), lambda _: ScriptedClient([]))
    # Состояние очереди проверяется до того, как цикл начнёт её разбирать.
    monkeypatch.setattr(processor, "_loop", lambda: None)
    with caplog.at_level("INFO", logger="rapporteur.processing"):
        processor.start()
    processor.stop()
    assert "После перезапуска приостановлены незавершённые задания" in caplog.text
    with database.read() as conn:
        talk = conn.execute(
            "SELECT job,step FROM talks WHERE id=?", (fixture["manual_talk"],)
        ).fetchone()
        assert tuple(talk) == ("paused", None)
        for table in ("preparations", "recommendation_jobs"):
            jobs = conn.execute(f"SELECT DISTINCT job FROM {table}").fetchall()
            assert [row["job"] for row in jobs] == ["paused"]
    # Очередь пуста: ScriptedClient без ответов упал бы при обращении к модели.
    assert not processor.run_once()


def test_upgrade_invalidates_locally_edited_preparation_drafts(tmp_path: Path) -> None:
    from rapporteur import db

    from .workflow_support import seed_regulation

    app = create_app(tmp_path, process=False)
    seed_regulation(app.state.database)
    with TestClient(app) as client:
        conference = client.post("/api/v1/conferences", json={"title": "Пример"}).json()["id"]
        base = f"/api/v1/conferences/{conference}"
        url = base + "/preparation"
        material = client.post(
            base + "/materials",
            params={"path": "речь.txt", "purpose": "document"},
            headers={"Content-Type": "application/octet-stream"},
            content="Текст доклада".encode(),
        )
        assert material.status_code == 201, material.text
        with app.state.database.tx() as conn:
            conn.execute(
                "INSERT INTO preparations(conference_id, version, job) VALUES (?, 4, 'done')",
                (conference,),
            )
        saved = client.put(
            url,
            json={
                "version": 4,
                "talks": [
                    {
                        "title": "Доклад",
                        "contacts": ["proposal@example.org"],
                        "links": [{"file_id": material.json()["id"]}],
                    }
                ],
            },
        )
        assert saved.status_code == 200, saved.text
        draft = {key: saved.json()[key] for key in ("version", "talks")}
        assert draft["version"] == 5
        talk_id = draft["talks"][0]["talk_id"]
        draft["talks"][0]["contacts"] = ["local-draft@example.org"]

    # Recreate a v4 card edit, which changed the registry without bumping preparation.version.
    with app.state.database.tx() as conn:
        conn.execute(
            "UPDATE talks SET contacts=?, input_revision=input_revision+1 WHERE id=?",
            (db.dumps(["registry@example.org"]), talk_id),
        )
        conn.execute("ALTER TABLE preparations DROP COLUMN auto_start")
        conn.execute("ALTER TABLE preparations DROP COLUMN input_snapshot")
        conn.execute("ALTER TABLE preparations DROP COLUMN review_reasons")
        conn.execute("ALTER TABLE talks DROP COLUMN worker_active")
        conn.execute("ALTER TABLE preparations DROP COLUMN workflow")
        conn.execute("ALTER TABLE preparations DROP COLUMN screening_confirmed")
        conn.execute("ALTER TABLE preparations DROP COLUMN context_file_ids")
        conn.execute("DROP TABLE department_details")
        conn.execute("DROP TABLE report_drafts")
        conn.execute("PRAGMA user_version = 4")

    with TestClient(create_app(tmp_path, process=False)) as client:
        card_url = f"/api/v1/talks/{talk_id}"
        before = client.get(card_url).json()
        assert client.put(url, json=draft).status_code == 409
        assert client.get(card_url).json() == before
        refreshed = client.get(url).json()
        assert refreshed["version"] == 6
        assert refreshed["talks"][0]["contacts"] == ["registry@example.org"]
        saved = client.put(url, json={key: refreshed[key] for key in ("version", "talks")})
        assert saved.status_code == 200, saved.text
        assert client.get(card_url).json()["contacts"] == ["registry@example.org"]
        saved_version = saved.json()["version"]

    with TestClient(create_app(tmp_path, process=False)) as client:
        assert client.get(url).json()["version"] == saved_version
