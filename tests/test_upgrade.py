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


def test_restart_requeues_all_interrupted_job_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        conn.execute("UPDATE preparations SET job='running'")
        conn.execute("UPDATE recommendation_jobs SET job='running'")
    processor = Processor(database, Storage(tmp_path), lambda _: ScriptedClient([]))
    # Inspect the restored queue before it starts consuming jobs.
    monkeypatch.setattr(processor, "_loop", lambda: None)
    processor.start()
    processor.stop()
    with database.read() as conn:
        talk = conn.execute(
            "SELECT job,step FROM talks WHERE id=?", (fixture["manual_talk"],)
        ).fetchone()
        assert tuple(talk) == ("queued", None)
        for table in ("preparations", "recommendation_jobs"):
            jobs = conn.execute(f"SELECT DISTINCT job FROM {table}").fetchall()
            assert [row["job"] for row in jobs] == ["queued"]
