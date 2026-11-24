"""HTTP endpoints for workflow."""

import json
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

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
    speakers: list[str]
    status: TalkStatus
    # queued, running, done, failed или paused — приостановлено после перезапуска до команды
    # оператора; step — текущий шаг: screening, parse, facts, verify, summary.
    job: Literal["queued", "running", "done", "failed", "paused"] | None
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
    auto_start: bool
    workflow: Literal["legacy", "operator"]
    screening_confirmed: bool
    files: int
    talks: list[TalkProgress]


@api.post("/conferences/{conference_id}/processing", status_code=202)
def start_processing(request: Request, conn: Tx, conference_id: str) -> Progress:
    materials.require_conference(conn, conference_id)
    _require_registry(conn, conference_id)
    if preparation.operator(conn, conference_id) and not preparation.screening_confirmed(
        conn, conference_id
    ):
        raise UserError("Сначала подтвердите соответствие отделам", 409)
    if not db.one(conn, "SELECT 1 FROM files WHERE conference_id = ?", conference_id):
        raise UserError("Сначала загрузите материалы")
    processing.start_conference(conn, conference_id)
    _wake(request)
    return conference_progress(conn, conference_id)


@api.get("/conferences/{conference_id}/progress")
def conference_progress(conn: ReadTx, conference_id: str) -> Progress:
    conference = materials.require_conference(conn, conference_id)
    automatic = preparation.automatic(conn, conference_id)
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
                speakers=[person["name"] for person in json.loads(row["speakers"])],
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
                    and (not automatic or interest.route(conn, row["id"]) is interest.Route.SUMMARY)
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
    operator = preparation.operator(conn, conference_id)
    if operator:
        active = {row["id"] for row in preparation.screening_talks(conn, conference_id)}
        talks = [talk for talk in talks if talk.id in active]
    files = conn.execute(
        "SELECT count(*) FROM files WHERE conference_id = ?", (conference_id,)
    ).fetchone()[0]
    return Progress(
        processing_started=bool(conference["processing_started"]),
        auto_start=automatic,
        workflow="operator" if operator else "legacy",
        screening_confirmed=preparation.screening_confirmed(conn, conference_id),
        files=files,
        talks=talks,
    )


@api.post("/talks/{talk_id}/retry", status_code=202)
def retry_talk(request: Request, conn: Tx, talk_id: str) -> Response:
    row = db.one(
        conn,
        "SELECT t.job,s.conference_id FROM talks t JOIN sections s ON s.id=t.section_id "
        "WHERE t.id=?",
        talk_id,
    )
    if row is None:
        raise NotFoundError("Доклад не найден")
    if preparation.operator(conn, row["conference_id"]) and (
        not preparation.screening_confirmed(conn, row["conference_id"])
        or talk_id not in {t["id"] for t in preparation.screening_talks(conn, row["conference_id"])}
    ):
        raise UserError("Сначала подтвердите реестр и соответствие отделам", 409)
    if row["job"] in ("queued", "running"):
        raise UserError("Доклад уже в обработке", 409)
    processing.enqueue(conn, [talk_id])
    _wake(request)
    return Response(status_code=202)


@api.get("/conferences/{conference_id}/preparation")
def preparation_state(conn: ReadTx, conference_id: str) -> dict[str, object]:
    return preparation.state(conn, conference_id)


@api.post("/conferences/{conference_id}/preparation", status_code=202)
def prepare_conference(
    request: Request, conn: Tx, conference_id: str, data: preparation.PreparationStart | None = None
) -> dict[str, object]:
    preparation.queue(
        conn,
        conference_id,
        auto_start=bool(data and data.auto_start),
        force=bool(data and data.force),
        workflow=data.workflow if data else "legacy",
    )
    _wake(request)
    return preparation.state(conn, conference_id)


@api.put("/conferences/{conference_id}/preparation")
def confirm_preparation(
    request: Request, conn: Tx, conference_id: str, data: preparation.ProposalWrite
) -> dict[str, object]:
    changed = preparation.apply(conn, conference_id, data, _storage(request))
    conference = materials.require_conference(conn, conference_id)
    if changed and (conference["processing_started"] or preparation.automatic(conn, conference_id)):
        processing.start_conference(conn, conference_id)
        _wake(request)
    return preparation.state(conn, conference_id)


def _require_registry(conn: ReadTx, conference_id: str) -> None:
    if preparation.operator(conn, conference_id) and not preparation.screening_talks(
        conn, conference_id
    ):
        raise UserError("Сначала подтвердите реестр докладов", 409)


def _screening_rows(conn: ReadTx, conference_id: str) -> list[db.Row]:
    if preparation.operator(conn, conference_id):
        return preparation.screening_talks(conn, conference_id)
    return db.conference_talks(conn, conference_id)


class ScreeningChoice(screening.Review):
    talk_id: str


class ScreeningConfirmation(BaseModel):
    talks: list[ScreeningChoice] = Field(max_length=500)


@api.put("/conferences/{conference_id}/screening")
def confirm_screening(
    request: Request, conn: Tx, conference_id: str, data: ScreeningConfirmation
) -> list[dict[str, object]]:
    materials.require_conference(conn, conference_id)
    _require_registry(conn, conference_id)
    if not preparation.operator(conn, conference_id):
        raise UserError("Этот реестр подтверждается отдельно для каждого доклада", 409)
    talks = _screening_rows(conn, conference_id)
    ids = {row["id"] for row in talks}
    choices = {choice.talk_id: choice for choice in data.talks}
    if len(choices) != len(data.talks) or choices.keys() - ids:
        raise UserError("Доклады отсутствуют в подтверждённом реестре или повторяются")
    for talk_id in ids:
        view = screening.view(conn, talk_id)
        if view["state"] in ("queued", "running", "paused", "not_started", "stale"):
            raise UserError("Дождитесь актуальной оценки всех докладов", 409)
        choice = choices.get(talk_id)
        if choice is None and not view["manual"]:
            raise UserError("Подтвердите решение по каждому докладу", 409)
        if choice is not None:
            same = view["manual"] and set(view["department_ids"]) == set(choice.department_ids)
            if same and view["version"] in (choice.version, choice.version + 1):
                continue
            screening.review(conn, talk_id, choice)
    conn.execute(
        "UPDATE preparations SET screening_confirmed=1 WHERE conference_id=?", (conference_id,)
    )
    for talk in talks:
        route = interest.processing_route(conn, talk["id"])
        has_card = bool(talk["card_only"])
        if (
            talk["job"] not in ("queued", "running", "done")
            or (talk["job"] == "done" and has_card != (route is interest.Route.CARD))
            or talk["status"] == "stale"
        ):
            processing.enqueue(conn, [talk["id"]])
    _wake(request)
    return conference_screening(conn, conference_id)


@api.get("/conferences/{conference_id}/screening")
def conference_screening(conn: ReadTx, conference_id: str) -> list[dict[str, object]]:
    materials.require_conference(conn, conference_id)
    return [
        {"talk_id": row["id"], **screening.view(conn, row["id"])}
        for row in _screening_rows(conn, conference_id)
    ]


@api.post("/conferences/{conference_id}/screening", status_code=202)
def rescreen(request: Request, conn: Tx, conference_id: str) -> list[dict[str, object]]:
    materials.require_conference(conn, conference_id)
    departments.require_regulations(conn)
    _require_registry(conn, conference_id)
    operator = preparation.operator(conn, conference_id)
    if operator:
        conn.execute(
            "UPDATE preparations SET screening_confirmed=0 WHERE conference_id=?", (conference_id,)
        )
    for talk in _screening_rows(conn, conference_id):
        if talk["status"] in ("matched", "stale", "processed", "reviewed"):
            view = screening.view(conn, talk["id"])
            if operator and (
                (view["manual"] and not view["stale"]) or view["state"] in ("queued", "running")
            ):
                continue
            screening.queue(conn, talk["id"], force=True)
            conn.execute("UPDATE talks SET job=NULL,step='screening' WHERE id=?", (talk["id"],))
    _wake(request)
    return conference_screening(conn, conference_id)


@api.post("/talks/{talk_id}/screening", status_code=202)
def rescreen_talk(request: Request, conn: Tx, talk_id: str) -> Response:
    """Пересчёт интереса отделов одного доклада: новая версия отбора ставится в очередь,
    остальные доклады конференции не затрагиваются. Требует положения и привязанных
    материалов; при запущенной обработке доклад ждёт нового отбора (как после rescreen)."""
    row = db.one(
        conn,
        "SELECT t.status, c.id AS conference_id, c.processing_started FROM talks t "
        "JOIN sections s ON "
        "t.section_id=s.id JOIN conferences c ON c.id=s.conference_id WHERE t.id=?",
        talk_id,
    )
    if row is None:
        raise NotFoundError("Доклад не найден")
    _require_registry(conn, row["conference_id"])
    if preparation.operator(conn, row["conference_id"]):
        if talk_id not in {
            t["id"] for t in preparation.screening_talks(conn, row["conference_id"])
        }:
            raise UserError("Доклад отсутствует в подтверждённом реестре", 409)
        conn.execute(
            "UPDATE preparations SET screening_confirmed=0 WHERE conference_id=?",
            (row["conference_id"],),
        )
        if screening.view(conn, talk_id)["state"] in ("queued", "running"):
            return Response(status_code=202)
    if row["status"] not in ("matched", "stale", "processed", "reviewed"):
        raise UserError("Пересчитать отбор можно для докладов с привязанными материалами", 409)
    screening.queue(conn, talk_id, force=True)
    if row["processing_started"]:
        conn.execute("UPDATE talks SET job=NULL,step='screening' WHERE id=?", (talk_id,))
    _wake(request)
    return Response(status_code=202)


@api.put("/talks/{talk_id}/screening")
def review_screening(
    request: Request, conn: Tx, talk_id: str, data: screening.Review
) -> dict[str, object]:
    row = db.one(
        conn,
        "SELECT c.id AS conference_id, c.processing_started FROM conferences c JOIN sections s ON "
        "s.conference_id=c.id JOIN talks t ON t.section_id=s.id WHERE t.id=?",
        talk_id,
    )
    if row and preparation.operator(conn, row["conference_id"]):
        raise UserError("Подтвердите решения на экране «Соответствие отделам»", 409)
    previous = interest.processing_route(conn, talk_id)
    screening.review(conn, talk_id, data)
    if row and row["processing_started"]:
        processing.enqueue_reviewed(conn, talk_id, previous)
        _wake(request)
    return screening.view(conn, talk_id)
