"""New workflows, with deterministic model responses and actual SQLite/API/storage."""

import io
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from docx import Document
from docx.enum.text import WD_BREAK
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from rapporteur import db, departments, model_choice, preparation
from rapporteur.app import create_app
from rapporteur.contracts import FragmentDraft, Summary
from rapporteur.contracts.enums import FragmentKind, ReportFormat, TextOrigin
from rapporteur.contracts.fragments import TimeLocator
from rapporteur.errors import UserError
from rapporteur.ingest.inventory import inspect_file
from rapporteur.llm.client import ScriptedClient
from rapporteur.report.assemble import (
    build_report_input,
    report_variables,
    select_talks,
    template_context,
)
from rapporteur.report.export import render_formats
from rapporteur.report.template import load_template
from rapporteur.reports import _sources
from rapporteur.settings import TEMPLATES_DIR

from .fictional_talk import SLIDES, facts_answer, summary_answer, supported_summary_answer
from .workflow_support import WorkflowClient, seed_regulation


@pytest.fixture
def web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, ScriptedClient]]:
    from rapporteur.llm.profiles import load_profile

    original_profile = load_profile
    monkeypatch.setattr(
        model_choice,
        "load_profile",
        lambda: original_profile().model_copy(update={"prompt_version": "v2"}),
    )
    scripted = WorkflowClient([])
    application = create_app(tmp_path, client=lambda _: scripted, process=False)
    seed_regulation(application.state.database)
    with TestClient(application) as client:
        yield client, scripted


def create_document_talk(client: TestClient, scripted: ScriptedClient) -> tuple[str, str]:
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
    material = client.get(base + "/materials").json()
    talk_id = material["talks"][0]["id"]
    saved = client.put(
        base + "/materials/bindings",
        json={"bindings": [{"source_file_id": uploaded.json()["id"], "talk_id": talk_id}]},
    )
    assert saved.status_code == 200, saved.text
    scripted.responses.extend(
        [
            re.sub(r'"S\d+"', '"D1"', facts_answer(listener_remarks=[])),
            re.sub(r'"S\d+"', '"D1"', facts_answer(listener_remarks=[])),
            summary_answer(),
            supported_summary_answer(),
        ]
    )
    assert client.post(base + "/processing").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    progress = client.get(base + "/progress").json()["talks"][0]
    assert progress["job"] == "done", progress
    return conference, talk_id


def test_document_only_processing_and_bulk_review(web: tuple[TestClient, ScriptedClient]) -> None:
    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    progress = client.get(f"/api/v1/conferences/{conference}/progress").json()["talks"][0]
    assert progress["bulk_reviewable"]
    summary = client.get(f"/api/v1/talks/{talk}/summary").json()
    assert summary["pack"]["fragments"][0]["label"] == "D1"
    assert summary["summary"]["status"] == "ready"
    result = client.post(
        f"/api/v1/conferences/{conference}/summaries/review",
        json={"items": [{"talk_id": talk, "version": summary["summary"]["version"]}]},
    )
    assert result.status_code == 200, result.text
    assert result.json()["confirmed"] == [talk]
    conflict = client.post(
        f"/api/v1/conferences/{conference}/summaries/review",
        json={"items": [{"talk_id": talk, "version": 1}]},
    )
    assert conflict.json()["skipped"][0]["talk_id"] == talk


def test_bulk_skips_visual_claims_and_wrong_conference(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    with client.app.state.database.tx() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT summary FROM summaries WHERE talk_id=?", (talk,)).fetchone()
        summary = json.loads(row["summary"])
        summary["claims"][0]["status"] = "needs_human"
        conn.execute("UPDATE summaries SET summary=? WHERE talk_id=?", (json.dumps(summary), talk))
    request = {"items": [{"talk_id": talk, "version": 1}]}
    result = client.post(f"/api/v1/conferences/{conference}/summaries/review", json=request)
    assert not result.json()["confirmed"]
    other = client.post("/api/v1/conferences", json={"title": "Другая"}).json()["id"]
    assert (
        client.post(f"/api/v1/conferences/{other}/summaries/review", json=request).status_code
        == 422
    )


def test_metadata_revision_conflicts(web: tuple[TestClient, ScriptedClient]) -> None:
    client, scripted = web
    _, talk = create_document_talk(client, scripted)
    url = f"/api/v1/talks/{talk}"
    metadata = client.get(url).json()
    value = {
        "title": "Исправленная тема",
        "speakers": [{"name": "Петров П. П."}],
        "revision": metadata["revision"],
    }
    assert client.put(url, json=value).status_code == 204
    assert client.put(url, json=value).status_code == 409
    assert client.get(url).json()["speakers"][0]["name"] == "Петров П. П."


def test_departments_references_review_staleness_and_export(
    web: tuple[TestClient, ScriptedClient], tmp_path: Path
) -> None:
    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    assert (
        client.post(f"/api/v1/talks/{talk}/summary/review", json={"version": 1}).status_code == 200
    )
    dep = client.post(
        "/api/v1/departments",
        params={"name": "Отдел хранения", "filename": "положение.txt"},
        content="Отдел исследует методы ускорения запросов в базах данных.".encode(),
        headers={"Content-Type": "application/octet-stream"},
    ).json()
    item = {
        "department_id": dep["id"],
        "department_version": 1,
        "reason": "Метод ускоряет запросы.",
        "claim_ids": ["c2"],
        "paragraph_ids": ["p1.1"],
    }
    scripted.responses.append(json.dumps({"items": [item]}, ensure_ascii=False))
    url = f"/api/v1/talks/{talk}/recommendations"
    assert client.post(url).status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    result = client.get(url).json()
    assert result["job"]["job"] == "done", result
    assert result["items"] == [item]
    assert client.put(url, json={"version": result["version"], "items": [item]}).status_code == 200
    database = client.app.state.database  # type: ignore[attr-defined]
    with database.tx() as conn:
        snapshot, _ = departments.report_snapshot(conn, conference)
        sources = _sources(conn, conference)
    assert snapshot[0]["paragraphs"]["p1.1"].startswith("Отдел исследует")
    assembled = build_report_input(sources, select_talks(sources), "starter-1", False)
    context = template_context(assembled.input, report_variables(assembled.input))
    rendered = render_formats(
        load_template(TEMPLATES_DIR / "starter"),
        context,
        [ReportFormat.DOCX, ReportFormat.MARKDOWN],
        tmp_path,
    )
    docx_text = "\n".join(
        p.text for p in Document(io.BytesIO(rendered[ReportFormat.DOCX])).paragraphs
    )
    assert "Доклады по интересам отделов" in docx_text and "p1.1:" in docx_text
    assert "Автор не установлен" in docx_text
    assert "Отдел хранения" in rendered[ReportFormat.MARKDOWN].decode()
    update = client.post(
        "/api/v1/departments",
        params={"name": "Отдел хранения", "filename": "новое.txt", "department_id": dep["id"]},
        content="Новая редакция положения.".encode(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert update.status_code == 201
    assert client.get(url).json()["stale"]
    with database.tx() as conn:
        assert not departments.report_snapshot(conn, conference)[0]
    assert snapshot[0]["department_version"] == 1


def test_invalid_department_reference_rejected(web: tuple[TestClient, ScriptedClient]) -> None:
    client, scripted = web
    _, talk = create_document_talk(client, scripted)
    with client.app.state.database.tx() as conn:  # type: ignore[attr-defined]
        summary = Summary.model_validate_json(
            conn.execute("SELECT summary FROM summaries WHERE talk_id=?", (talk,)).fetchone()[0]
        )
    with pytest.raises(UserError):
        departments.validate(
            [
                departments.Recommendation(
                    department_id="unknown",
                    department_version=1,
                    reason="x",
                    claim_ids=["c999"],
                    paragraph_ids=["p1.1"],
                )
            ],
            summary,
            [],
        )


def test_images_and_docx_are_not_programs(tmp_path: Path) -> None:
    image = tmp_path / "slide.png"
    Image.new("RGB", (64, 64), "white").save(image)
    assert inspect_file(image).kind.value == "image"
    document = tmp_path / "speech.docx"
    doc = Document()
    doc.add_paragraph("Текст выступления")
    doc.save(str(document))
    assert inspect_file(document).kind.value == "document"
    assert inspect_file(document, program=True).kind.value == "program"


def test_link_validation_and_no_neighbouring_speech() -> None:
    with pytest.raises(ValidationError):
        preparation.Link(file_id="x", start_ms=2, end_ms=1)
    with pytest.raises(ValidationError):
        preparation.Link(file_id="x", page_start=1)
    fragments = [
        FragmentDraft(
            kind=FragmentKind.TRANSCRIPT,
            source_sha256="a" * 64,
            locator=TimeLocator(kind="time", start_ms=i * 1000, end_ms=(i + 1) * 1000),
            raw_text=str(i),
            normalized_text=str(i),
            origin=TextOrigin.ASR,
            engine_confidence=None,
        )
        for i in range(3)
    ]
    selected = preparation.slice_fragments(
        fragments, preparation.Link(file_id="x", start_ms=1000, end_ms=2000)
    )
    assert [f.normalized_text for f in selected] == ["1"]


def test_migration_preserves_old_data_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO conferences(id,title,created_at) VALUES ('a','Сохранить','now')")
    database = db.Database(path)
    db.Database(path)
    with database.tx() as conn:
        assert (
            conn.execute("SELECT title FROM conferences WHERE id='a'").fetchone()[0] == "Сохранить"
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    with sqlite3.connect(str(path) + ".before-v1.bak") as conn:
        assert (
            conn.execute("SELECT title FROM conferences WHERE id='a'").fetchone()[0] == "Сохранить"
        )


def test_inflight_result_does_not_overwrite_new_inputs(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, scripted = web
    conference, talk = create_document_talk(client, scripted)
    processor = client.app.state.processor  # type: ignore[attr-defined]
    database = client.app.state.database  # type: ignore[attr-defined]
    with database.tx() as conn:
        conn.execute("UPDATE talks SET job='queued' WHERE id=?", (talk,))
    taken = processor._take()
    assert taken is not None
    summary, pack = processor._process(taken)
    with database.tx() as conn:
        conn.execute("UPDATE talks SET input_revision=input_revision+1 WHERE id=?", (talk,))
    processor._save(talk, summary, pack, taken.revision)
    progress = client.get(f"/api/v1/conferences/{conference}/progress").json()["talks"][0]
    assert progress["job"] == "queued" and progress["summary_number"] == 1


def test_recording_proposal_ranges_are_atomic_and_isolated(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, _ = web
    conference = client.post("/api/v1/conferences", json={"title": "Запись"}).json()["id"]
    database = client.app.state.database  # type: ignore[attr-defined]
    from uuid import uuid4

    file_id = str(uuid4())
    with database.tx() as conn:
        conn.execute(
            "INSERT INTO "
            "files(id,conference_id,sha256,original_name,relative_path,kind,"
            "media_type,size_bytes,duration_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (file_id, conference, "a" * 64, "x.wav", "x.wav", "audio", "audio/wav", 1, 10000),
        )
        conn.execute(
            "INSERT INTO preparations(conference_id,version,job) VALUES (?,1,'done')", (conference,)
        )
    url = f"/api/v1/conferences/{conference}/preparation"
    proposal = {
        "version": 1,
        "talks": [
            {"title": "Первый", "links": [{"file_id": file_id, "start_ms": 0, "end_ms": 5000}]},
            {"title": "Второй", "links": [{"file_id": file_id, "start_ms": 4000, "end_ms": 10000}]},
        ],
    }
    assert client.put(url, json=proposal).status_code == 422
    assert not client.get(f"/api/v1/conferences/{conference}/progress").json()["talks"]
    proposal["talks"][1]["links"][0]["start_ms"] = 5000  # type: ignore[index]
    response = client.put(url, json=proposal)
    assert response.status_code == 200, response.text
    assert client.put(url, json=proposal).status_code == 409
    talks = response.json()["talks"]
    review = client.put(
        f"/api/v1/conferences/{conference}/materials/bindings",
        json={"bindings": [{"source_file_id": file_id, "talk_id": None}]},
    )
    assert all(t["status"] == "matched" for t in review.json()["talks"])
    assert len(review.json()["files"][0]["range_talk_ids"]) == 2
    proposal = {"version": response.json()["version"], "talks": talks[:1]}
    assert client.put(url, json=proposal).status_code == 200
    with database.tx() as conn:
        assert (
            conn.execute("SELECT status FROM talks WHERE id=?", (talks[1]["talk_id"],)).fetchone()[
                0
            ]
            == "insufficient_data"
        )
        assert conn.execute("SELECT count(*) FROM material_links").fetchone()[0] == 1


def test_preparation_worker_proposes_unbound_document_and_restores_queue(
    web: tuple[TestClient, ScriptedClient],
) -> None:
    client, scripted = web
    conference = client.post("/api/v1/conferences", json={"title": "Без папок"}).json()["id"]
    base = f"/api/v1/conferences/{conference}"
    assert (
        client.post(
            base + "/materials",
            params={"path": "речь.txt", "purpose": "document"},
            headers={"Content-Type": "application/octet-stream"},
            content="Тема доклада: кэширование. Иванов И. И.".encode(),
        ).status_code
        == 201
    )
    scripted.responses.append(
        json.dumps(
            {
                "title": "Кэширование",
                "speakers": ["Иванов И. И."],
                "talk_index": None,
                "confidence": 1,
                "reason": "Текст выступления",
            }
        )
    )
    assert client.post(base + "/preparation").status_code == 202
    assert client.app.state.processor.run_once()  # type: ignore[attr-defined]
    state = client.get(base + "/preparation").json()
    assert state["job"] == "done", state
    assert state["talks"][0]["title"] == "Кэширование"
    result = client.put(
        base + "/preparation", json={"version": state["version"], "talks": state["talks"]}
    )
    assert result.status_code == 200, result.text
    assert client.get(base + "/progress").json()["talks"][0]["status"] == "matched"


def test_long_documents_are_batched_cached_and_keep_evidence(tmp_path: Path) -> None:
    from uuid import uuid4

    from rapporteur.ingest.documents import fragment
    from rapporteur.storage import Storage
    from rapporteur.summarize.facts import FactsConfig, extract_facts
    from rapporteur.summarize.pack import PackSource, build_pack

    from .fictional_talk import PROFILE

    text = "\n".join(SLIDES)
    drafts = [
        fragment("a" * 64, i, text + "\n" + "дополнение " * 700, TextOrigin.EXTRACTED)
        for i in range(1, 5)
    ]
    pack = build_pack(uuid4(), uuid4(), [PackSource(uuid4(), drafts)])
    # Use a bounded profile so two source pages fit per request, and reduction is separate.
    profile = PROFILE.model_copy(
        update={
            "generation": PROFILE.generation.model_copy(
                update={"context_tokens": 16000, "max_tokens": 2000}
            )
        }
    )
    responses = [re.sub(r'"S\d+"', f'"D{i}"', facts_answer(listener_remarks=[])) for i in (1, 3, 1)]
    scripted = ScriptedClient(responses)
    config = FactsConfig(profile=profile)
    artifact = extract_facts(scripted, pack, config, Storage(tmp_path))
    assert artifact.facts.results[0].evidence[0].label == "D1"
    assert len(scripted.requests) > 1
    count = len(scripted.requests)
    assert extract_facts(scripted, pack, config, Storage(tmp_path)) == artifact
    assert len(scripted.requests) == count


def test_word_extraction_has_real_page_locators(tmp_path: Path) -> None:
    from rapporteur.ingest.documents import pages

    document = Document()
    document.add_paragraph("Первый доклад. Иванов И. И. Метод исследования.")
    document.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    document.add_paragraph("Второй доклад. Петров П. П. Результаты исследования.")
    buffer = io.BytesIO()
    document.save(buffer)
    extracted = pages(
        buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert len(extracted) == 2
    assert "Иванов" in extracted[0][0] and "Петров" in extracted[1][0]
    assert all(
        image.startswith(b"\x89PNG") and origin is TextOrigin.EXTRACTED
        for _, image, origin in extracted
    )
