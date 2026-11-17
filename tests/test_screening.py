"""Department interest controls admission to every processing entry point."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from rapporteur import db, materials, model_choice, processing
from rapporteur.app import create_app
from rapporteur.contracts import ModelProfile
from rapporteur.errors import UserError
from rapporteur.llm.client import ScriptedClient
from rapporteur.storage import Storage

from .fictional_talk import PROFILE, pack


def setup_talk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[db.Database, Storage, str, str]:
    monkeypatch.setattr(model_choice, "load_profile", lambda: PROFILE)
    database = db.Database(tmp_path / "data.db")
    conference = db.new_id()
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO conferences(id,title,created_at) VALUES (?,?,?)",
            (conference, "Пример", db.now()),
        )
        talk = materials.add_talk(conn, conference, "Кэширование запросов", None)
        conn.execute("UPDATE talks SET status='matched' WHERE id=?", (talk,))
    return database, Storage(tmp_path / "files"), conference, talk


def add_regulation(database: db.Database, version: int = 1) -> str:
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO departments VALUES (?,?,?,?,?,?,?)",
            (
                "databases",
                version,
                "Отдел баз данных",
                "положение.txt",
                "a" * 64,
                db.dumps([{"page": 1, "text": "Отдел исследует методы снижения задержки чтения."}]),
                db.now(),
            ),
        )
    return "databases"


def decision(status: str = "relevant") -> str:
    return json.dumps(
        {
            "status": status,
            "reason": "Работа со снижением задержки чтения",
            "evidence": [{"label": "S2", "quote": "снизить задержку чтения"}],
            "paragraphs": [{"label": "p1.1", "quote": "методы снижения задержки чтения"}],
        }
        if status == "relevant"
        else {"status": status, "reason": "Связь не установлена", "evidence": [], "paragraphs": []}
    )


def test_material_upload_requires_department_regulations(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path, process=False)) as client:
        cid = client.post("/api/v1/conferences", json={"title": "Пример"}).json()["id"]
        response = client.post(
            f"/api/v1/conferences/{cid}/materials",
            params={"path": "речь.txt"},
            headers={"Content-Type": "application/octet-stream"},
            content=b"text",
        )
        assert response.status_code == 409
        assert response.json()["code"] == "departments_required"
        assert client.get(f"/api/v1/conferences/{cid}/materials").json()["files"] == []


def test_enqueue_requires_usable_regulations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, _, _, talk = setup_talk(tmp_path, monkeypatch)
    with database.tx() as conn, pytest.raises(UserError):
        processing.enqueue(conn, [talk])


@pytest.mark.parametrize(
    "status,eligible,queued",
    # Без интереса отделов доклад обрабатывается до карточки, без фактов и сводки
    # (interest.py); неопределённый ждёт решения оператора.
    [("relevant", True, True), ("not_relevant", False, True), ("uncertain", False, False)],
)
def test_interest_selects_only_supported_relevant_talks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str, eligible: bool, queued: bool
) -> None:
    from rapporteur import screening

    database, storage, _, talk = setup_talk(tmp_path, monkeypatch)
    add_regulation(database)
    with database.tx() as conn:
        processing.enqueue(conn, [talk])
        assert not screening.view(conn, talk)["eligible"]
        row = db.one(conn, "SELECT job FROM talks WHERE id=?", talk)
        assert row is not None and row["job"] is None
    client = ScriptedClient([decision(status)])
    source = pack().model_copy(update={"talk_id": UUID(talk)})
    assert screening.run_once(database, storage, lambda _: client, lambda _: source) == talk
    with database.tx() as conn:
        assert screening.view(conn, talk)["eligible"] is eligible
        processing.enqueue(conn, [talk])
        row = db.one(conn, "SELECT job FROM talks WHERE id=?", talk)
        assert row is not None
        assert (row["job"] == "queued") is queued


def test_regulation_change_invalidates_screening_and_manual_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur import screening

    database, storage, _, talk = setup_talk(tmp_path, monkeypatch)
    add_regulation(database)
    with database.tx() as conn:
        processing.enqueue(conn, [talk])
    screening.run_once(
        database,
        storage,
        lambda _: ScriptedClient([decision("uncertain")]),
        lambda _: pack().model_copy(update={"talk_id": UUID(talk)}),
    )
    with database.tx() as conn:
        current = screening.view(conn, talk)
        screening.review(
            conn, talk, screening.Review(version=current["version"], department_ids=["databases"])
        )
        assert screening.view(conn, talk)["eligible"]
        with pytest.raises(UserError):
            screening.review(
                conn, talk, screening.Review(version=current["version"], department_ids=[])
            )
    add_regulation(database, 2)
    with database.read() as conn:
        assert screening.view(conn, talk)["stale"]
        assert not screening.view(conn, talk)["eligible"]
        assert (
            conn.execute(
                "SELECT count(*) FROM talk_screenings WHERE talk_id=?", (talk,)
            ).fetchone()[0]
            == 2
        )


def test_source_chunk_labels_are_unique_and_preserve_text() -> None:
    from rapporteur.screening import _chunks

    text = "Начало важного факта. " * 200
    batches = list(_chunks([{"label": "S1", "text": text}], 1000))
    assert all(len({p["label"] for p in batch}) == len(batch) for batch in batches)
    assert "".join(p["text"] for batch in batches for p in batch) == text


def test_failed_screening_can_be_requeued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rapporteur import screening

    database, _, _, talk = setup_talk(tmp_path, monkeypatch)
    add_regulation(database)
    with database.tx() as conn:
        processing.enqueue(conn, [talk])
        conn.execute(
            "UPDATE talk_screenings SET job='failed',error='сбой' WHERE talk_id=?", (talk,)
        )
        processing.enqueue(conn, [talk])
        assert screening.view(conn, talk)["state"] == "queued"


def test_screening_cache_and_input_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur import screening

    database, storage, _, talk = setup_talk(tmp_path, monkeypatch)
    add_regulation(database)
    source = pack().model_copy(update={"talk_id": UUID(talk)})
    client = ScriptedClient([decision("not_relevant")])
    for _ in range(2):
        with database.tx() as conn:
            screening.queue(conn, talk, force=True)
        screening.run_once(database, storage, lambda _: client, lambda _: source)
    assert len(client.requests) == 1
    with database.tx() as conn:
        conn.execute("UPDATE talks SET input_revision=input_revision+1 WHERE id=?", (talk,))
        assert screening.view(conn, talk)["state"] == "stale"
        processing.enqueue(conn, [talk])
        assert screening.view(conn, talk)["state"] == "queued"


def test_filtered_talk_gets_card_without_facts_or_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur.processing import Processor

    database, storage, _, talk = setup_talk(tmp_path, monkeypatch)
    add_regulation(database)
    client = ScriptedClient([decision("not_relevant")])
    worker = Processor(database, storage, lambda _: client)
    source = pack().model_copy(update={"talk_id": UUID(talk)})
    monkeypatch.setattr(worker, "_screen_pack", lambda _: source)
    monkeypatch.setattr(worker, "_pack", lambda _: source)
    with database.tx() as conn:
        processing.enqueue(conn, [talk])
    assert worker.run_once()
    assert not worker.run_once()
    # Единственный запрос к модели — отбор; фактов и сводки нет.
    assert len(client.requests) == 1
    with database.read() as conn:
        row = db.one(conn, "SELECT job, card_only FROM talks WHERE id=?", talk)
        summaries = conn.execute("SELECT count(*) FROM summaries").fetchone()[0]
    assert row is not None and (row["job"], row["card_only"], summaries) == ("done", 1, 0)


def test_screening_failure_keeps_departments_available_for_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur import screening

    database, _, _, talk = setup_talk(tmp_path, monkeypatch)
    add_regulation(database)
    with database.tx() as conn:
        screening.queue(conn, talk)
        conn.execute(
            "UPDATE talk_screenings SET job='failed',error='Недоступна модель' WHERE talk_id=?",
            (talk,),
        )
        current = screening.view(conn, talk)
        assert current["items"][0]["department_id"] == "databases"
        screening.review(
            conn, talk, screening.Review(version=current["version"], department_ids=["databases"])
        )
        assert screening.view(conn, talk)["eligible"]


def test_recovered_preparation_requires_regulations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rapporteur.processing import Processor

    database, storage, conference, _ = setup_talk(tmp_path, monkeypatch)
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO preparations(conference_id,version,job) VALUES (?,1,'queued')",
            (conference,),
        )
    clients: list[ModelProfile] = []

    def client(profile: ModelProfile) -> ScriptedClient:
        clients.append(profile)
        return ScriptedClient([])

    worker = Processor(database, storage, client)
    assert worker.run_once()
    assert not clients
    with database.read() as conn:
        row = db.one(conn, "SELECT job,error FROM preparations WHERE conference_id=?", conference)
        assert row is not None and row["job"] == "failed"
        assert "положение" in row["error"]


def test_changing_purpose_requires_regulations(tmp_path: Path) -> None:
    from .workflow_support import seed_regulation

    application = create_app(tmp_path, process=False)
    seed_regulation(application.state.database)
    with TestClient(application) as client:
        cid = client.post("/api/v1/conferences", json={}).json()["id"]
        uploaded = client.post(
            f"/api/v1/conferences/{cid}/materials",
            params={"path": "speech.txt"},
            content="Текст доклада для разбора".encode(),
            headers={"Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 201
        fid = client.get(f"/api/v1/conferences/{cid}/materials").json()["files"][0]["source"]["id"]
        with application.state.database.tx() as conn:
            conn.execute("DELETE FROM departments")
        result = client.put(f"/api/v1/files/{fid}/purpose", json={"purpose": "program"})
        assert result.status_code == 409
        assert result.json()["code"] == "departments_required"
