"""Persistent report builds with immutable input and template snapshots."""

import logging
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rapporteur import db, departments, interest, preparation, reports, screening
from rapporteur.contracts import ReportInput
from rapporteur.errors import NotFoundError, UserError
from rapporteur.report.assemble import (
    ReportError,
    build_report_input,
    report_variables,
    template_context,
)
from rapporteur.report.build import OPERATOR_TEMPLATE, REPORT_FORMATS, BuiltReport
from rapporteur.report.export import export_files, render_formats
from rapporteur.report.template import Template, TemplateError, TemplateSpec, load_template
from rapporteur.settings import TEMPLATES_DIR
from rapporteur.storage import Storage, cache_key, sha256_hex

log = logging.getLogger("rapporteur.report_jobs")
TEMPLATE_DIR = TEMPLATES_DIR / "starter"


class BuildRequest(reports.ReportBuild):
    idempotency_key: str = Field(min_length=1, max_length=100)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: ReportInput
    warnings: list[str]
    template: TemplateSpec
    template_key: str
    template_sha256: str
    source_versions: dict[str, int]
    input_revisions: dict[str, int]
    interest_versions: dict[str, int]
    draft_revision: int | None = None


def view(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = db.one(conn, "SELECT * FROM report_jobs WHERE id=?", job_id)
    if row is None:
        raise NotFoundError("Задание выпуска не найдено")
    snapshot = Snapshot.model_validate_json(row["snapshot"])
    return {
        key: row[key]
        for key in (
            "id",
            "conference_id",
            "status",
            "step",
            "error",
            "attempt",
            "report_version_id",
            "created_at",
            "updated_at",
        )
    } | {
        "source_versions": snapshot.source_versions,
        "template_sha256": snapshot.template_sha256,
    }


def list_jobs(conn: sqlite3.Connection, conference_id: str) -> list[dict[str, Any]]:
    from rapporteur.materials import require_conference

    require_conference(conn, conference_id)
    return [
        view(conn, row["id"])
        for row in conn.execute(
            "SELECT id FROM report_jobs WHERE conference_id=? ORDER BY created_at DESC",
            (conference_id,),
        )
    ]


def _existing(
    conn: sqlite3.Connection, conference_id: str, key: str, request_hash: str
) -> dict[str, Any] | None:
    row = db.one(
        conn,
        "SELECT id,request_hash FROM report_jobs WHERE conference_id=? AND idempotency_key=?",
        conference_id,
        key,
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise UserError("Этот ключ выпуска уже использован с другими параметрами.", 409)
    return view(conn, row["id"])


def enqueue(
    database: db.Database, storage: Storage, conference_id: str, data: BuildRequest
) -> dict[str, Any]:
    excluded = {"idempotency_key"}
    if data.draft_revision is None:
        excluded.add("draft_revision")
    request_hash = cache_key(data.model_dump(exclude=excluded))
    with database.read() as conn:
        existing = _existing(conn, conference_id, data.idempotency_key, request_hash)
        if existing:
            return existing
        departments.require_regulations(conn)
        operator = preparation.operator(conn, conference_id)
        selection_request = data
        if not operator and data.talk_ids is None:
            selection_request = data.model_copy(
                update={
                    "talk_ids": [
                        UUID(row["id"])
                        for row in db.conference_talks(conn, conference_id)
                        if interest.reportable(conn, row["id"])
                    ]
                }
            )
        # Сначала отбор: без него доклад не получил ни сводку, ни карточку (interest.py).
        own = {row["id"] for row in db.conference_talks(conn, conference_id)}
        if any(
            str(talk_id) in own and not interest.reportable(conn, str(talk_id))
            for talk_id in selection_request.talk_ids or []
        ):
            raise UserError(
                "Для выбранных докладов подтвердите актуальный интерес отделов "
                "и дождитесь их обработки.",
                409,
                code="screening_required",
            )
        sources, selection = reports.select_input(conn, conference_id, selection_request)
        interests = {
            str(talk_id): screening.view(conn, str(talk_id)) for talk_id in selection.talk_ids
        }
        input_revisions = {
            row["id"]: row["input_revision"]
            for row in db.conference_talks(conn, conference_id)
            if row["id"] in interests
        }
    try:
        directory = TEMPLATES_DIR / OPERATOR_TEMPLATE if operator else TEMPLATE_DIR
        template = load_template(directory)
    except TemplateError as error:
        raise UserError(f"Не удалось прочитать бланк справки: {error}", 500) from error
    content = template.document.read_bytes()
    sha = sha256_hex(content)
    template_key = f"report-templates/{sha}.docx"
    storage.put(template_key, content)
    try:
        assembled = build_report_input(
            sources, selection, template.spec.version, data.unreviewed_confirmed and not operator
        )
    except ReportError as error:
        raise UserError(f"Справка не собрана: {error}") from error
    if operator and assembled.input.skipped:
        raise UserError("Проверьте сводки всех включённых докладов перед выпуском", 409)
    snapshot = Snapshot(
        input=assembled.input,
        warnings=assembled.warnings,
        template=template.spec,
        template_key=template_key,
        template_sha256=sha,
        source_versions={
            str(s.talk_id): s.version for s in sources.summaries if str(s.talk_id) in interests
        },
        input_revisions=input_revisions,
        interest_versions={key: value["version"] for key, value in interests.items()},
        draft_revision=data.draft_revision,
    )
    with database.tx() as conn:
        existing = _existing(conn, conference_id, data.idempotency_key, request_hash)
        if existing:
            return existing
        _require_current(conn, conference_id, snapshot)
        job_id = db.new_id()
        now = db.now()
        conn.execute(
            "INSERT INTO "
            "report_jobs(id,conference_id,idempotency_key,request_hash,"
            "snapshot,status,step,created_at,updated_at) VALUES "
            "(?,?,?,?,?,'queued','queued',?,?)",
            (
                job_id,
                conference_id,
                data.idempotency_key,
                request_hash,
                snapshot.model_dump_json(),
                now,
                now,
            ),
        )
        return view(conn, job_id)


def _require_current(conn: sqlite3.Connection, conference_id: str, snapshot: Snapshot) -> None:
    if snapshot.input.operator_fields is not None:
        reports.require_operator_snapshot(
            conn, conference_id, snapshot.input, snapshot.draft_revision
        )
    revisions = {
        row["id"]: row["input_revision"] for row in db.conference_talks(conn, conference_id)
    }
    summaries = db.current_summaries(conn, conference_id)
    for talk_id, revision in snapshot.input_revisions.items():
        summary_version = summaries[talk_id]["number"] if talk_id in summaries else None
        if (
            revisions.get(talk_id) != revision
            or not interest.reportable(conn, talk_id)
            or screening.view(conn, talk_id)["version"] != snapshot.interest_versions[talk_id]
            or summary_version != snapshot.source_versions.get(talk_id)
        ):
            raise UserError("Состав справки изменился. Соберите новую справку.", 409)


def retry(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    current = view(conn, job_id)
    # Приостановленное после перезапуска задание продолжается тем же снимком (processing.py).
    if current["status"] not in ("failed", "paused"):
        raise UserError("Повтор доступен для неудачного или приостановленного задания.", 409)
    if preparation.automatic(conn, current["conference_id"]) or preparation.operator(
        conn, current["conference_id"]
    ):
        stored = db.one(conn, "SELECT snapshot FROM report_jobs WHERE id=?", job_id)
        assert stored is not None
        _require_current(
            conn, current["conference_id"], Snapshot.model_validate_json(stored["snapshot"])
        )
    conn.execute(
        "UPDATE report_jobs SET status='queued',step='queued',error=NULL,updated_at=? WHERE id=?",
        (db.now(), job_id),
    )
    return view(conn, job_id)


def run_once(database: db.Database, storage: Storage) -> bool:
    with database.tx() as conn:
        row = db.one(
            conn, "SELECT * FROM report_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        )
        if row is None:
            return False
        job_id, attempt = row["id"], row["attempt"] + 1
        conn.execute(
            "UPDATE report_jobs SET "
            "status='running',step='docx',attempt=?,error=NULL,updated_at=? WHERE id=?",
            (attempt, db.now(), job_id),
        )
        log.info("Выпуск справки: задание %s, попытка %s", job_id, attempt)
    try:
        snapshot = Snapshot.model_validate_json(row["snapshot"])
        with database.read() as conn:
            if snapshot.input.operator_fields is not None or preparation.automatic(
                conn, row["conference_id"]
            ):
                _require_current(conn, row["conference_id"], snapshot)
        if sha256_hex(storage.get(snapshot.template_key)) != snapshot.template_sha256:
            raise UserError("Сохранённый бланк повреждён.")
        template = Template(spec=snapshot.template, document=storage.path(snapshot.template_key))
        variables = report_variables(snapshot.input)

        def progress(step: str) -> None:
            with database.tx() as conn:
                conn.execute(
                    "UPDATE report_jobs SET step=?,updated_at=? WHERE id=? AND "
                    "status='running' AND attempt=?",
                    (step, db.now(), job_id, attempt),
                )

        with tempfile.TemporaryDirectory(prefix="report-job-") as directory:
            rendered = render_formats(
                template,
                template_context(snapshot.input, variables),
                REPORT_FORMATS,
                Path(directory),
                progress=progress,
            )
        progress("save")
        files = export_files(storage, f"reports/{job_id}/attempt-{attempt}", rendered)
        built = BuiltReport(snapshot.input, variables, files, snapshot.warnings)
        with database.tx() as conn:
            current = db.one(conn, "SELECT status,attempt FROM report_jobs WHERE id=?", job_id)
            if current is None or current["status"] != "running" or current["attempt"] != attempt:
                return True
            if snapshot.input.operator_fields is not None or preparation.automatic(
                conn, row["conference_id"]
            ):
                _require_current(conn, row["conference_id"], snapshot)
            reports.save_version(conn, row["conference_id"], job_id, built)
            conn.execute(
                "UPDATE report_jobs SET "
                "status='done',step='done',report_version_id=?,updated_at=? WHERE "
                "id=? AND attempt=?",
                (job_id, db.now(), job_id, attempt),
            )
    except Exception as error:
        log.exception("Выпуск справки, задание %s", job_id)
        with database.tx() as conn:
            conn.execute(
                "UPDATE report_jobs SET status='failed',error=?,updated_at=? WHERE "
                "id=? AND status='running' AND attempt=?",
                (
                    f"Задание {job_id}: {type(error).__name__}: {error}"[:2000],
                    db.now(),
                    job_id,
                    attempt,
                ),
            )
    return True
