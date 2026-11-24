"""Проверка и правка сводки человеком (spec §6.8, проверка 3).

Каждая правка и каждое подтверждение создают новую версию сводки. Запрос передаёт номер
версии, которую видел человек: если сводку уже изменили, правка отклоняется. Цитаты и метки
проверяются по пакету фрагментов той версии, из которой выросла правка.
"""

import datetime as dt
import sqlite3
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError

from rapporteur import db, interest
from rapporteur.contracts import (
    Summary,
    SummaryLine,
    TalkFragmentPack,
    TalkSummaryBundle,
    can_transition,
)
from rapporteur.contracts.base import NonEmptyStr
from rapporteur.contracts.claims import ClaimKey
from rapporteur.contracts.entities import Speaker
from rapporteur.contracts.enums import ClaimStatus, SummaryStatus, TalkStatus
from rapporteur.errors import NotFoundError, UserError


class ClaimEdit(BaseModel):
    """Новый текст утверждения: правка снимает с него подтверждение."""

    model_config = ConfigDict(extra="forbid")

    key: ClaimKey
    text: NonEmptyStr


class SummaryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: PositiveInt
    sentences: list[SummaryLine] = Field(min_length=1)
    conclusions: list[SummaryLine] = Field(max_length=3)
    claims: list[ClaimEdit] = Field(default_factory=list)


class SummaryReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: PositiveInt


class TalkSummaryView(BaseModel):
    summary: Summary
    pack: TalkFragmentPack
    talk_title: str | None
    talk_status: TalkStatus
    speakers: list[Speaker] = Field(default_factory=list)
    conference_id: str = ""
    job: str | None = None


def _talk(conn: sqlite3.Connection, talk_id: str) -> db.Row:
    row = db.one(conn, "SELECT * FROM talks WHERE id = ?", talk_id)
    if row is None:
        raise NotFoundError("Доклад не найден")
    return row


def _current(conn: sqlite3.Connection, talk_id: str) -> db.Row:
    row = db.one(
        conn, "SELECT * FROM summaries WHERE talk_id = ? ORDER BY number DESC LIMIT 1", talk_id
    )
    if row is None:
        raise NotFoundError("У доклада ещё нет сводки")
    return row


def view(conn: sqlite3.Connection, talk_id: str) -> TalkSummaryView:
    talk = _talk(conn, talk_id)
    row = _current(conn, talk_id)
    section = db.one(conn, "SELECT conference_id FROM sections WHERE id=?", talk["section_id"])
    assert section is not None
    return TalkSummaryView(
        summary=Summary.model_validate_json(row["summary"]),
        pack=TalkFragmentPack.model_validate_json(row["pack"]),
        talk_title=talk["title"],
        talk_status=talk["status"],
        speakers=db.talk(talk).speakers,
        conference_id=section["conference_id"],
        job=talk["job"],
    )


def _locked(conn: sqlite3.Connection, talk_id: str, version: int) -> tuple[db.Row, db.Row]:
    talk = _talk(conn, talk_id)
    row = _current(conn, talk_id)
    if row["number"] != version:
        raise UserError("Сводку уже изменили: обновите её и повторите правку", 409)
    return talk, row


def _save(conn: sqlite3.Connection, talk_id: str, row: db.Row, values: dict[str, Any]) -> None:
    """Новая версия поверх текущей; согласованность сводки и пакета проверяет контракт."""
    number = row["number"] + 1
    pack = TalkFragmentPack.model_validate_json(row["pack"])
    created = dt.datetime.now(dt.UTC)
    try:
        summary = Summary.model_validate(
            {
                **Summary.model_validate_json(row["summary"]).model_dump(mode="json"),
                **values,
                "id": db.new_id(),
                "version": number,
                "human_edit": True,
                "created_at": created.isoformat(),
            }
        )
        TalkSummaryBundle(pack=pack, summary=summary)
    except ValidationError as error:
        problems = "; ".join(item["msg"] for item in error.errors())
        raise UserError(f"Правка не согласуется с утверждениями сводки: {problems}") from error
    conn.execute(
        "INSERT INTO summaries (talk_id, number, summary, pack, created_at) VALUES (?, ?, ?, ?, ?)",
        (talk_id, number, summary.model_dump_json(), row["pack"], created.isoformat()),
    )


def edit(conn: sqlite3.Connection, talk_id: str, data: SummaryEdit) -> None:
    talk, row = _locked(conn, talk_id, data.version)
    current = Summary.model_validate_json(row["summary"])
    texts = {item.key: item.text for item in data.claims}
    unknown = set(texts) - {claim.key for claim in current.claims}
    if unknown:
        raise UserError(f"Неизвестные утверждения: {', '.join(sorted(unknown))}")
    claims = []
    for claim in current.claims:
        changed = claim.key in texts and texts[claim.key] != claim.text
        if changed and claim.status is ClaimStatus.REJECTED:
            raise UserError("Утверждение без подтверждённой цитаты не правится: его нет в сводке")
        if changed:
            claim = claim.model_copy(
                update={"text": texts[claim.key], "status": ClaimStatus.NEEDS_HUMAN}
            )
        claims.append(claim.model_dump(mode="json"))
    _save(
        conn,
        talk_id,
        row,
        {
            "claims": claims,
            "sentences": [line.model_dump(mode="json") for line in data.sentences],
            "conclusions": [line.model_dump(mode="json") for line in data.conclusions],
            "status": SummaryStatus.READY.value,
        },
    )
    # Правка проверенной сводки снимает проверку доклада.
    if talk["status"] == TalkStatus.REVIEWED.value:
        conn.execute(
            "UPDATE talks SET status = ? WHERE id = ?", (TalkStatus.PROCESSED.value, talk_id)
        )


def review(conn: sqlite3.Connection, talk_id: str, data: SummaryReview) -> None:
    """Человек проверил сводку: утверждения из её текста подтверждены, доклад проверен."""
    talk, row = _locked(conn, talk_id, data.version)
    if (
        interest.automatic(conn, talk_id)
        and interest.route(conn, talk_id) is not interest.Route.SUMMARY
    ):
        raise UserError("Сначала подтвердите назначение доклада отделам", 409)
    if talk["job"] in ("queued", "running"):
        raise UserError("Дождитесь завершения обработки", 409)
    status = TalkStatus(talk["status"])
    if not can_transition(status, TalkStatus.REVIEWED):
        raise UserError(f"Доклад в статусе {status.value} нельзя отметить проверенным")
    summary = Summary.model_validate_json(row["summary"])
    if not summary.sentences:
        raise UserError("В сводке нет текста: сначала допишите её")
    used = {key for line in summary.sentences + summary.conclusions for key in line.claim_ids}
    claims = [
        claim.model_copy(update={"status": ClaimStatus.CONFIRMED})
        if claim.key in used and claim.status is not ClaimStatus.REJECTED
        else claim
        for claim in summary.claims
    ]
    _save(
        conn,
        talk_id,
        row,
        {
            "claims": [claim.model_dump(mode="json") for claim in claims],
            "status": SummaryStatus.READY.value,
        },
    )
    conn.execute("UPDATE talks SET status = ? WHERE id = ?", (TalkStatus.REVIEWED.value, talk_id))


class BulkItem(BaseModel):
    talk_id: str
    version: PositiveInt


class BulkReview(BaseModel):
    items: list[BulkItem] = Field(max_length=500)


def bulk_review(conn: sqlite3.Connection, conference_id: str, data: BulkReview) -> dict[str, Any]:
    from rapporteur.materials import require_conference

    require_conference(conn, conference_id)
    allowed = {r["id"] for r in db.conference_talks(conn, conference_id)}
    if len({i.talk_id for i in data.items}) != len(data.items) or any(
        i.talk_id not in allowed for i in data.items
    ):
        raise UserError("Повторяющийся доклад или доклад другой конференции")
    confirmed, skipped = [], []
    for item in data.items:
        try:
            talk, row = _locked(conn, item.talk_id, item.version)
            summary = Summary.model_validate_json(row["summary"])
            if (
                talk["status"] != "processed"
                or talk["job"] in ("queued", "running")
                or summary.status is not SummaryStatus.READY
                or not summary.sentences
                or any(c.status is ClaimStatus.NEEDS_HUMAN for c in summary.claims)
            ):
                raise UserError("Доклад требует индивидуальной проверки или ещё обрабатывается")
            review(conn, item.talk_id, SummaryReview(version=item.version))
            confirmed.append(item.talk_id)
        except UserError as error:
            skipped.append({"talk_id": item.talk_id, "reason": error.message})
    return {"confirmed": confirmed, "skipped": skipped}
