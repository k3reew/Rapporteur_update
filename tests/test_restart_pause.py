"""После перезапуска задания ждут оператора и продолжаются его командой (processing.py)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapporteur import db, processing
from rapporteur.app import create_app

from .fictional_talk import SLIDES
from .workflow_support import WorkflowClient, seed_regulation


@pytest.fixture
def web(tmp_path: Path) -> Iterator[tuple[TestClient, db.Database]]:
    scripted = WorkflowClient([])
    application = create_app(tmp_path, client=lambda _: scripted, process=False)
    seed_regulation(application.state.database)
    with TestClient(application) as client:
        yield client, application.state.database


def bound_talk(client: TestClient) -> tuple[str, str]:
    conference = client.post("/api/v1/conferences", json={"title": "Пример"}).json()["id"]
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


def restart(database: db.Database) -> dict[str, int]:
    with database.tx() as conn:
        return processing.pause_unfinished(conn)


def test_waiting_screening_is_paused_and_continues_from_the_talk(
    web: tuple[TestClient, db.Database],
) -> None:
    client, database = web
    conference, talk_id = bound_talk(client)
    assert client.post(f"/api/v1/conferences/{conference}/processing").status_code == 202
    assert restart(database)["отбор"] == 1

    progress = client.get(f"/api/v1/conferences/{conference}/progress").json()["talks"][0]
    assert progress["screening"]["state"] == "paused"
    assert client.app.state.processor.run_once() is False  # type: ignore[attr-defined]
    assert client.get("/api/v1/system").json()["queue"] == {}

    # «Продолжить» у доклада: отбор ставится заново новой версией.
    assert client.post(f"/api/v1/talks/{talk_id}/retry").status_code == 202
    screening = client.get(f"/api/v1/conferences/{conference}/screening").json()[0]
    assert (screening["state"], screening["version"]) == ("queued", 2)


def test_interrupted_talk_is_paused_and_continues_on_retry(
    web: tuple[TestClient, db.Database],
) -> None:
    client, database = web
    conference, talk_id = bound_talk(client)
    with database.tx() as conn:
        conn.execute("UPDATE talks SET job='running', step='facts' WHERE id=?", (talk_id,))
    assert restart(database)["доклады"] == 1

    talk = client.get(f"/api/v1/conferences/{conference}/progress").json()["talks"][0]
    assert (talk["job"], talk["step"]) == ("paused", None)
    assert client.get("/api/v1/system").json()["queue"] == {"paused": 1}
    assert client.post(f"/api/v1/talks/{talk_id}/retry").status_code == 202
    talk = client.get(f"/api/v1/conferences/{conference}/progress").json()["talks"][0]
    assert talk["job"] is None and talk["step"] == "screening"


def test_paused_preparation_and_recommendations_continue_on_request(
    web: tuple[TestClient, db.Database],
) -> None:
    client, database = web
    conference, talk_id = bound_talk(client)
    base = f"/api/v1/conferences/{conference}"
    assert client.post(base + "/preparation").status_code == 202
    with database.tx() as conn:
        conn.execute("UPDATE talks SET status='reviewed' WHERE id=?", (talk_id,))
        conn.execute(
            "INSERT INTO recommendation_jobs(talk_id, job) VALUES (?, 'running')", (talk_id,)
        )
    paused = restart(database)
    assert (paused["подготовка"], paused["рекомендации"]) == (1, 1)
    assert client.get(base + "/preparation").json()["job"] == "paused"
    assert client.get(f"/api/v1/talks/{talk_id}/recommendations").json()["job"]["job"] == "paused"

    assert client.post(base + "/preparation").json()["job"] == "queued"
    assert client.post(f"/api/v1/talks/{talk_id}/recommendations").status_code == 202
    assert client.get(f"/api/v1/talks/{talk_id}/recommendations").json()["job"]["job"] == "queued"
