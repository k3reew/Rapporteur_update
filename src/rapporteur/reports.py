"""Сборка справки из текущих данных конференции и её версии (spec §6.9, ADR-006).

Справка собирается по запросу пользователя прямо в запросе: DOCX по бланку и PDF через
LibreOffice занимают секунды. Каждая сборка — новая версия со снимком входа.
"""

import datetime as dt
import json
import sqlite3
import tempfile
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rapporteur import db, departments, interest, preparation
from rapporteur.contracts import ReportInput, ReportSelection, ReportVersion, Speaker, Summary
from rapporteur.contracts.enums import REPORTABLE_TALK_STATUSES, TalkStatus
from rapporteur.contracts.reports import ReportContact, ReportDraftFields, ReportFile
from rapporteur.errors import NotFoundError, UserError
from rapporteur.materials import require_conference
from rapporteur.report.assemble import (
    ReportError,
    ReportSources,
    build_report_input,
    suggested_contacts,
)
from rapporteur.report.build import BuiltReport, build_report
from rapporteur.storage import Storage


class ReportBuild(BaseModel):
    """Непроверенные доклады входят только с явным подтверждением (ADR-006). Без списка
    докладов в справку идут все доклады конференции."""

    model_config = ConfigDict(extra="forbid")

    unreviewed_confirmed: bool = False
    talk_ids: list[UUID] | None = None
    draft_revision: int | None = Field(default=None, ge=0)


class ReportVersionView(ReportVersion):
    warnings: list[str]


class ReportDraftWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    fields: ReportDraftFields


class ReportPreviewTalk(BaseModel):
    id: UUID
    speakers: list[Speaker]
    organizations: list[str]
    contacts: list[str]
    annotation: list[str]
    conclusions: list[str]


class ReportDraftView(ReportDraftWrite):
    suggested_contacts: list[ReportContact]
    talks: list[ReportPreviewTalk]


def _draft(conn: sqlite3.Connection, conference_id: str) -> ReportDraftWrite:
    row = db.one(
        conn, "SELECT revision,fields FROM report_drafts WHERE conference_id=?", conference_id
    )
    if row is None:
        return ReportDraftWrite(revision=0, fields=ReportDraftFields())
    return ReportDraftWrite(
        revision=row["revision"], fields=ReportDraftFields.model_validate_json(row["fields"])
    )


def draft(conn: sqlite3.Connection, conference_id: str) -> ReportDraftView:
    sources = _sources(conn, conference_id)
    value = _draft(conn, conference_id)
    talks = []
    for talk in sources.ordered_talks():
        summary = None if talk.card_only else sources.summary_by_talk.get(talk.id)
        talks.append(
            ReportPreviewTalk(
                id=talk.id,
                speakers=talk.speakers,
                organizations=talk.organizations,
                contacts=talk.contacts,
                annotation=[line.text for line in summary.sentences] if summary else [],
                conclusions=[line.text for line in summary.conclusions] if summary else [],
            )
        )
    return ReportDraftView(
        revision=value.revision,
        fields=value.fields,
        suggested_contacts=suggested_contacts(sources, sources.contact_talk_ids),
        talks=talks,
    )


def save_draft(
    conn: sqlite3.Connection, conference_id: str, data: ReportDraftWrite
) -> ReportDraftView:
    require_conference(conn, conference_id)
    previous = _draft(conn, conference_id)
    if data.revision != previous.revision:
        raise UserError("Черновик справки изменился. Откройте актуальную версию.", 409)
    if data.fields != previous.fields:
        conn.execute(
            "INSERT INTO report_drafts (conference_id,revision,fields,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(conference_id) DO UPDATE SET revision=excluded.revision,"
            "fields=excluded.fields,updated_at=excluded.updated_at",
            (conference_id, previous.revision + 1, data.fields.model_dump_json(), db.now()),
        )
    return draft(conn, conference_id)


def _sources(conn: sqlite3.Connection, conference_id: str) -> ReportSources:
    conference = db.conference(require_conference(conn, conference_id))
    sections = conn.execute(
        "SELECT * FROM sections WHERE conference_id = ?", (conference_id,)
    ).fetchall()
    operator = preparation.operator(conn, conference_id)
    recommendations, warnings = (
        ([], []) if operator else departments.report_snapshot(conn, conference_id)
    )
    talks = [db.talk(row) for row in db.conference_talks(conn, conference_id)]
    if not operator and preparation.automatic(conn, conference_id):
        for i, talk in enumerate(talks):
            if talk.card_only:
                talks[i] = talk.model_copy(
                    update={
                        "speakers": [
                            person.model_copy(update={"affiliation": None, "contacts": []})
                            for person in talk.speakers
                        ],
                        "organizations": [],
                        "contacts": [],
                    }
                )
    return ReportSources(
        department_recommendations=recommendations,
        recommendation_warnings=warnings,
        conference=conference,
        sections=[db.section(row) for row in sections],
        talks=talks,
        summaries=[
            Summary.model_validate_json(row["summary"])
            for row in db.current_summaries(conn, conference_id).values()
        ],
        operator_fields=_draft(conn, conference_id).fields if operator else None,
        contact_talk_ids=frozenset(
            talk.id
            for talk in talks
            if interest.route(conn, str(talk.id)) is interest.Route.SUMMARY
        )
        if operator
        else frozenset(),
    )


def _check(sources: ReportSources, selection: ReportSelection, unreviewed_confirmed: bool) -> None:
    """Частые причины отказа проверяются заранее, чтобы человек сразу видел, что исправить."""
    talks = {talk.id: talk for talk in sources.talks if talk.id in selection.talk_ids}
    summarized = {
        s.talk_id
        for s in sources.summaries
        if s.talk_id in talks and s.sentences and not talks[s.talk_id].card_only
    }
    # Доклад только с карточкой входит в справку без сводки (interest.py).
    cards = {
        talk_id
        for talk_id, talk in talks.items()
        if talk.card_only and talk.status in REPORTABLE_TALK_STATUSES
    }
    if not summarized and not cards:
        raise UserError("Ни у одного выбранного доклада нет готовой сводки или карточки")
    unreviewed = [
        talk_id for talk_id in summarized if talks[talk_id].status is not TalkStatus.REVIEWED
    ]
    if unreviewed and not unreviewed_confirmed:
        raise UserError("В справке есть непроверенные доклады: подтвердите сборку без проверки")
    dated = any(sources.talk_day(talks[talk_id]) for talk_id in summarized | cards)
    if sources.operator_fields is None and not (
        selection.days or dated or sources.conference.start_date
    ):
        raise UserError("Укажите даты конференции: без них в справке нет периода проведения")


def build(
    database: db.Database, storage: Storage, conference_id: str, data: ReportBuild
) -> ReportVersionView:
    with database.read() as conn:
        sources, selection = select_input(conn, conference_id, data)

    version_id = db.new_id()
    try:
        with tempfile.TemporaryDirectory(prefix="report-") as work:
            built = build_report(
                sources,
                selection,
                data.unreviewed_confirmed and sources.operator_fields is None,
                storage,
                f"reports/{version_id}",
                work=Path(work),
            )
    except ReportError as error:
        raise UserError(f"Справка не собрана: {error}") from error

    with database.tx() as conn:
        if sources.operator_fields is not None:
            if built.input.skipped:
                raise UserError("Проверьте сводки всех включённых докладов перед выпуском", 409)
            require_operator_snapshot(conn, conference_id, built.input, data.draft_revision)
        elif preparation.automatic(conn, conference_id):
            selected = set(selection.talk_ids)
            current, _ = select_input(
                conn,
                conference_id,
                data.model_copy(
                    update={
                        "talk_ids": selection.talk_ids,
                    }
                ),
            )
            if [talk for talk in current.talks if talk.id in selected] != [
                talk for talk in sources.talks if talk.id in selected
            ] or [s for s in current.summaries if s.talk_id in selected] != [
                s for s in sources.summaries if s.talk_id in selected
            ]:
                raise UserError("Состав справки изменился. Соберите новую справку.", 409)
        return save_version(conn, conference_id, version_id, built)


def select_input(
    conn: sqlite3.Connection, conference_id: str, data: ReportBuild
) -> tuple[ReportSources, ReportSelection]:
    sources = _sources(conn, conference_id)
    ordered = [talk.id for talk in sources.ordered_talks()]
    talk_ids = ordered if data.talk_ids is None else data.talk_ids
    automatic = preparation.automatic(conn, conference_id)
    operator = sources.operator_fields is not None
    if operator:
        if not preparation.screening_confirmed(conn, conference_id):
            raise UserError("Подтвердите решения по отделам перед выпуском справки", 409)
        if (
            data.draft_revision is not None
            and data.draft_revision != _draft(conn, conference_id).revision
        ):
            raise UserError("Черновик справки изменился. Откройте актуальную версию.", 409)
        registry = {row["id"] for row in preparation.screening_talks(conn, conference_id)}
        eligible = [
            talk.id
            for talk in sources.ordered_talks()
            if str(talk.id) in registry
            and talk.status not in (TalkStatus.NOT_HELD, TalkStatus.INSUFFICIENT_DATA)
        ]
        if data.talk_ids is not None and (
            len(data.talk_ids) != len(eligible) or set(data.talk_ids) != set(eligible)
        ):
            raise UserError("Состав реестра изменился. Откройте актуальный предпросмотр.", 409)
        talk_ids = eligible
    elif automatic and data.talk_ids is None:
        talk_ids = [talk_id for talk_id in talk_ids if interest.reportable(conn, str(talk_id))]
    if not talk_ids:
        raise UserError("В конференции нет докладов")
    if set(talk_ids) - set(ordered):
        raise UserError("В выборке есть доклад другой конференции")
    if (automatic or operator) and any(
        not interest.reportable(conn, str(talk_id)) for talk_id in talk_ids
    ):
        raise UserError("Подтвердите отбор и проверьте сводки выбранных докладов", 409)
    selection = ReportSelection(days=[], section_ids=[], talk_ids=talk_ids)
    _check(sources, selection, data.unreviewed_confirmed and not (automatic or operator))

    return sources, selection


def require_operator_snapshot(
    conn: sqlite3.Connection, conference_id: str, snapshot: ReportInput, revision: int | None
) -> None:
    current, selection = select_input(
        conn,
        conference_id,
        ReportBuild(talk_ids=snapshot.selection.talk_ids, draft_revision=revision),
    )
    try:
        assembled = build_report_input(current, selection, snapshot.template_version, False)
    except ReportError as error:
        raise UserError("Состав справки изменился. Соберите новую справку.", 409) from error
    if assembled.input != snapshot:
        raise UserError("Состав справки изменился. Соберите новую справку.", 409)


def save_version(
    conn: sqlite3.Connection, conference_id: str, version_id: str, built: BuiltReport
) -> ReportVersionView:
    created = dt.datetime.now(dt.UTC)
    require_conference(conn, conference_id)
    number = conn.execute(
        "SELECT coalesce(max(number), 0) + 1 FROM report_versions WHERE conference_id = ?",
        (conference_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO report_versions "
        "(id, conference_id, number, input, variables, files, warnings, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            conference_id,
            number,
            built.input.model_dump_json(),
            built.variables.model_dump_json(),
            db.dumps([file.model_dump(mode="json") for file in built.files]),
            db.dumps(built.warnings),
            created.isoformat(),
        ),
    )
    row = db.one(conn, "SELECT * FROM report_versions WHERE id = ?", version_id)
    assert row is not None
    return version(row)


def version(row: db.Row) -> ReportVersionView:
    return ReportVersionView.model_validate(
        {
            "id": row["id"],
            "conference_id": row["conference_id"],
            "number": row["number"],
            "input": json.loads(row["input"]),
            "variables": json.loads(row["variables"]),
            "files": json.loads(row["files"]),
            "warnings": json.loads(row["warnings"]),
            "created_at": row["created_at"],
        }
    )


def versions(conn: sqlite3.Connection, conference_id: str) -> list[ReportVersionView]:
    require_conference(conn, conference_id)
    rows = conn.execute(
        "SELECT * FROM report_versions WHERE conference_id = ? ORDER BY number DESC",
        (conference_id,),
    ).fetchall()
    return [version(row) for row in rows]


def report_file(
    conn: sqlite3.Connection, version_id: str, report_format: str
) -> tuple[int, ReportFile]:
    row = db.one(conn, "SELECT number, files FROM report_versions WHERE id = ?", version_id)
    if row is None:
        raise NotFoundError("Версия справки не найдена")
    files = [ReportFile.model_validate(item) for item in json.loads(row["files"])]
    file = next((item for item in files if item.format.value == report_format), None)
    if file is None:
        raise NotFoundError("В этой версии нет такого формата")
    return row["number"], file
