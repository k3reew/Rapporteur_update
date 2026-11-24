"""Отбор отделов определяет вид черновика, решение оператора определяет допуск к выпуску."""

import sqlite3
from enum import StrEnum
from typing import Any

from rapporteur import db, screening
from rapporteur.contracts.enums import REPORTABLE_TALK_STATUSES, TalkStatus


class Route(StrEnum):
    SUMMARY = "summary"
    CARD = "card"


def decided(view: dict[str, Any]) -> Route | None:
    """Итог отбора по его представлению screening.view(); None — решения ещё нет или
    решение модели не подтверждено оператором."""
    if not view.get("manual"):
        return None
    match view["state"]:
        case "relevant":
            return Route.SUMMARY
        case "not_relevant":
            return Route.CARD
    return None


def route(conn: sqlite3.Connection, talk_id: str) -> Route | None:
    return decided(screening.view(conn, talk_id))


def automatic(conn: sqlite3.Connection, talk_id: str) -> bool:
    row = db.one(
        conn,
        "SELECT p.auto_start FROM talks t JOIN sections s ON s.id=t.section_id "
        "JOIN preparations p ON p.conference_id=s.conference_id WHERE t.id=?",
        talk_id,
    )
    return bool(row and row["auto_start"])


def processing_route(conn: sqlite3.Connection, talk_id: str) -> Route | None:
    view = screening.view(conn, talk_id)
    row = db.one(
        conn,
        "SELECT p.auto_start,p.workflow,p.screening_confirmed,p.job FROM preparations p "
        "JOIN sections s ON s.conference_id=p.conference_id JOIN talks t ON t.section_id=s.id "
        "WHERE t.id=?",
        talk_id,
    )
    if row and row["workflow"] == "operator":
        return decided(view) if row["screening_confirmed"] and row["job"] == "confirmed" else None
    if row and row["auto_start"] and view["state"] == "relevant":
        return Route.SUMMARY
    return decided(view)


def reportable(conn: sqlite3.Connection, talk_id: str) -> bool:
    """Доклад входит в справку, когда его обработка соответствует актуальному отбору:
    сводка у интересного отделам доклада, карточка у остальных. Наличие и состояние самой
    сводки проверяет сборка справки (reports.py, report/assemble.py)."""
    row = db.one(conn, "SELECT status, card_only, job FROM talks WHERE id = ?", talk_id)
    if row is None:
        return False
    auto = automatic(conn, talk_id)
    if auto and row["job"] != "done":
        return False
    match route(conn, talk_id):
        case Route.SUMMARY:
            return not row["card_only"] and (not auto or row["status"] == TalkStatus.REVIEWED)
        case Route.CARD:
            return bool(row["card_only"]) and TalkStatus(row["status"]) in REPORTABLE_TALK_STATUSES
    return False
