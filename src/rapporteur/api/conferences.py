"""HTTP endpoints for conferences."""

import datetime as dt
import sqlite3
from typing import Literal, Self

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, model_validator

from rapporteur import (
    db,
    materials,
    preparation,
)
from rapporteur.api.dependencies import ReadTx, Tx
from rapporteur.contracts.base import NonEmptyStr
from rapporteur.errors import NotFoundError

api = APIRouter()


class ConferenceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyStr | None = None
    start_date: dt.date | None = None
    end_date: dt.date | None = None
    place: NonEmptyStr | None = None
    organizer: NonEmptyStr | None = None
    workflow: Literal["legacy", "operator"] = "legacy"

    @model_validator(mode="after")
    def _dates(self) -> Self:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Дата окончания раньше начала")
        return self


class ConferenceItem(BaseModel):
    id: str
    title: str | None
    start_date: dt.date | None
    end_date: dt.date | None
    place: str | None
    organizer: str | None
    processing_started: bool
    talks: int
    summaries: int
    reviewed: int
    resume_step: Literal["materials", "talks", "screening", "review", "report"]
    workflow: Literal["legacy", "operator"]


_CONFERENCE_ITEM = """
SELECT conferences.*,
    coalesce((SELECT p.workflow FROM preparations p
              WHERE p.conference_id=conferences.id), 'legacy') AS workflow,
    (SELECT count(*) FROM talks JOIN sections ON sections.id = talks.section_id
     WHERE sections.conference_id = conferences.id) AS talks,
    (SELECT count(DISTINCT summaries.talk_id) FROM summaries
     JOIN talks ON talks.id = summaries.talk_id JOIN sections ON sections.id = talks.section_id
     WHERE sections.conference_id = conferences.id) AS summaries,
    (SELECT count(*) FROM talks JOIN sections ON sections.id = talks.section_id
     WHERE sections.conference_id = conferences.id AND talks.status = 'reviewed') AS reviewed,
    CASE
        WHEN EXISTS (SELECT 1 FROM preparations p WHERE p.conference_id=conferences.id
                     AND p.job='awaiting_start') THEN 'materials'
        WHEN EXISTS (SELECT 1 FROM preparations p WHERE p.conference_id=conferences.id
                     AND p.job<>'confirmed') THEN 'talks'
        WHEN EXISTS (SELECT 1 FROM files f WHERE f.conference_id=conferences.id
                     AND f.confirmed=0
                     AND (f.kind NOT IN ('audio','video') OR NOT EXISTS (
                         SELECT 1 FROM preparations p WHERE p.conference_id=conferences.id
                         AND p.workflow='operator'
                     ))) THEN 'materials'
        WHEN EXISTS (SELECT 1 FROM report_jobs r WHERE r.conference_id=conferences.id
                     AND r.status IN ('queued','running','paused','failed')) THEN 'report'
        WHEN EXISTS (SELECT 1 FROM talks t JOIN sections s ON s.id=t.section_id
                     WHERE s.conference_id=conferences.id
                     AND (t.status IN ('matched','stale')
                          OR t.job IN ('queued','running','failed','paused')))
                     AND conferences.processing_started=1 THEN 'review'
        WHEN EXISTS (SELECT 1 FROM report_versions r WHERE r.conference_id=conferences.id)
                     THEN 'report'
        WHEN conferences.processing_started=1 THEN 'review'
        WHEN EXISTS (SELECT 1 FROM preparations p WHERE p.conference_id=conferences.id) THEN 'talks'
        ELSE 'materials'
    END AS resume_step
FROM conferences
"""


def _conference_item(conn: sqlite3.Connection, row: db.Row) -> ConferenceItem:
    item = ConferenceItem.model_validate(dict(row))
    if item.workflow == "operator" and item.resume_step not in ("materials", "talks"):
        if not preparation.screening_confirmed(conn, item.id):
            item.resume_step = "screening"
        elif all(talk["job"] == "done" for talk in preparation.screening_talks(conn, item.id)):
            item.resume_step = "report"
    return item


def _conference(conn: sqlite3.Connection, conference_id: str) -> ConferenceItem:
    row = db.one(conn, _CONFERENCE_ITEM + "WHERE conferences.id = ?", conference_id)
    if row is None:
        raise NotFoundError("Конференция не найдена")
    return _conference_item(conn, row)


@api.get("/conferences")
def list_conferences(conn: ReadTx) -> list[ConferenceItem]:
    rows = conn.execute(_CONFERENCE_ITEM + "ORDER BY created_at DESC").fetchall()
    return [_conference_item(conn, row) for row in rows]


@api.post("/conferences", status_code=201)
def create_conference(conn: Tx, data: ConferenceWrite) -> ConferenceItem:
    conference_id = db.new_id()
    conn.execute(
        "INSERT INTO conferences (id, title, start_date, end_date, place, organizer, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conference_id, *_conference_values(data), db.now()),
    )
    if data.workflow == "operator":
        conn.execute(
            "INSERT INTO preparations(conference_id,job,workflow,auto_start) "
            "VALUES (?,'awaiting_start','operator',1)",
            (conference_id,),
        )
    return _conference(conn, conference_id)


def _conference_values(data: ConferenceWrite) -> tuple[str | None, ...]:
    def day(value: dt.date | None) -> str | None:
        return value.isoformat() if value else None

    return data.title, day(data.start_date), day(data.end_date), data.place, data.organizer


@api.get("/conferences/{conference_id}")
def get_conference(conn: ReadTx, conference_id: str) -> ConferenceItem:
    return _conference(conn, conference_id)


@api.put("/conferences/{conference_id}")
def update_conference(conn: Tx, conference_id: str, data: ConferenceWrite) -> ConferenceItem:
    materials.require_conference(conn, conference_id)
    conn.execute(
        "UPDATE conferences SET title = ?, start_date = ?, end_date = ?, place = ?, organizer = ? "
        "WHERE id = ?",
        (*_conference_values(data), conference_id),
    )
    return _conference(conn, conference_id)


@api.delete("/conferences/{conference_id}", status_code=204)
def delete_conference(conn: Tx, conference_id: str) -> Response:
    materials.require_conference(conn, conference_id)
    conn.execute("DELETE FROM conferences WHERE id = ?", (conference_id,))
    return Response(status_code=204)
