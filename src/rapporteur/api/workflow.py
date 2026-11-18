"""HTTP endpoints for workflow."""

import json
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from rapporteur import (
    db,
    departments,
    interest,
    materials,
    preparation,
    processing,
    screening,
)
from rapporteur.api.dependencies import ReadTx, Tx, _storage, _wake
from rapporteur.contracts.enums import SummaryStatus, TalkStatus
from rapporteur.errors import NotFoundError, UserError

api = APIRouter()


class TalkProgress(BaseModel):
    id: str
    section_id: str
    section: str
    order: int
    title: str | None
    status: TalkStatus
    # queued, running, done или failed; step — текущий шаг: parse, facts, verify, summary.
    job: Literal["queued", "running", "done", "failed"] | None
    step: str | None
    error: str | None
    summary_number: int | None
    summary_status: SummaryStatus | None
    screening: dict[str, object] | None = None
    bulk_reviewable: bool = False
    # Итог обработки: докладу без интереса отделов достаточно карточки (interest.py).
    card_only: bool = False
    # Обработка соответствует актуальному отбору, доклад можно выбрать для справки.
    reportable: bool = False


class Progress(BaseModel):
    processing_started: bool
    files: int
    talks: list[TalkProgress]


@api.post("/conferences/{conference_id}/processing", status_code=202)
def start_processing(request: Request, conn: Tx, conference_id: str) -> Progress:
    materials.require_conference(conn, conference_id)
    if not db.one(conn, "SELECT 1 FROM files WHERE conference_id = ?", conference_id):
        raise UserError("Сначала загрузите материалы")
    departments.require_regulations(conn)
    conn.execute("UPDATE conferences SET processing_started = 1 WHERE id = ?", (conference_id,))
    processing.enqueue_ready(conn, conference_id)
    _wake(request)
    return conference_progress(conn, conference_id)


@api.get("/conferences/{conference_id}/progress")
def conference_progress(conn: ReadTx, conference_id: str) -> Progress:
    conference = materials.require_conference(conn, conference_id)
    current = db.current_summaries(conn, conference_id)
    talks = []
    for row in db.conference_talks(conn, conference_id):
        summary = current.get(row["id"])
        talks.append(
            TalkProgress(
                id=row["id"],
                section_id=row["section_id"],
                section=row["section_title"],
                order=row["position"],
                title=row["title"],
                status=row["status"],
                job=row["job"],
                step=row["step"],
                error=row["error"],
                card_only=bool(row["card_only"]),
                reportable=interest.reportable(conn, row["id"]),
                summary_number=summary["number"] if summary else None,
                summary_status=json.loads(summary["summary"])["status"] if summary else None,
                screening=screening.view(conn, row["id"]),
                bulk_reviewable=bool(
                    summary
                    and row["status"] == "processed"
                    and row["job"] not in ("queued", "running")
                    and json.loads(summary["summary"])["status"] == "ready"
                    and json.loads(summary["summary"])["sentences"]
                    and not any(
                        c["status"] == "needs_human"
                        for c in json.loads(summary["summary"])["claims"]
                    )
                ),
            )
        )
    files = conn.execute(
        "SELECT count(*) FROM files WHERE conference_id = ?", (conference_id,)
    ).fetchone()[0]
    return Progress(
        processing_started=bool(conference["processing_started"]), files=files, talks=talks
    )


@api.post("/talks/{talk_id}/retry", status_code=202)
def retry_talk(request: Request, conn: Tx, talk_id: str) -> Response:
    row = db.one(conn, "SELECT job FROM talks WHERE id = ?", talk_id)
    if row is None:
        raise NotFoundError("Доклад не найден")
    if row["job"] in ("queued", "running"):
        raise UserError("Доклад уже в обработке", 409)
    processing.enqueue(conn, [talk_id])
    _wake(request)
    return Response(status_code=202)


@api.get("/conferences/{conference_id}/preparation")
def preparation_state(conn: ReadTx, conference_id: str) -> dict[str, object]:
    return preparation.state(conn, conference_id)


@api.post("/conferences/{conference_id}/preparation", status_code=202)
def prepare_conference(request: Request, conn: Tx, conference_id: str) -> dict[str, object]:
    preparation.queue(conn, conference_id)
    _wake(request)
    return preparation.state(conn, conference_id)


@api.put("/conferences/{conference_id}/preparation")
def confirm_preparation(
    request: Request, conn: Tx, conference_id: str, data: preparation.ProposalWrite
) -> dict[str, object]:
    preparation.apply(conn, conference_id, data, _storage(request))
    conference = materials.require_conference(conn, conference_id)
    if conference["processing_started"]:
        processing.enqueue_ready(conn, conference_id)
        _wake(request)
    return preparation.state(conn, conference_id)


@api.get("/conferences/{conference_id}/screening")
def conference_screening(conn: ReadTx, conference_id: str) -> list[dict[str, object]]:
    materials.require_conference(conn, conference_id)
    return [
        {"talk_id": row["id"], **screening.view(conn, row["id"])}
        for row in db.conference_talks(conn, conference_id)
    ]


@api.post("/conferences/{conference_id}/screening", status_code=202)
def rescreen(request: Request, conn: Tx, conference_id: str) -> list[dict[str, object]]:
    materials.require_conference(conn, conference_id)
    departments.require_regulations(conn)
    for talk in db.conference_talks(conn, conference_id):
        if talk["status"] in ("matched", "stale", "processed", "reviewed"):
            screening.queue(conn, talk["id"], force=True)
            conn.execute("UPDATE talks SET job=NULL,step='screening' WHERE id=?", (talk["id"],))
    _wake(request)
    return conference_screening(conn, conference_id)


@api.put("/talks/{talk_id}/screening")
def review_screening(
    request: Request, conn: Tx, talk_id: str, data: screening.Review
) -> dict[str, object]:
    screening.review(conn, talk_id, data)
    row = db.one(
        conn,
        "SELECT c.processing_started FROM conferences c JOIN sections s ON "
        "s.conference_id=c.id JOIN talks t ON t.section_id=s.id WHERE t.id=?",
        talk_id,
    )
    if row and row["processing_started"]:
        processing.enqueue(conn, [talk_id])
        _wake(request)
    return screening.view(conn, talk_id)
