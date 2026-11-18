"""Сводка или только карточка: итог отбора по интересу отделов (screening.py).

Доклад, интересный хотя бы одному отделу, получает краткую сводку. Остальным достаточно
карточки: темы, авторов, организаций и контактов. Решение принимает предварительный отбор
модели, оператор переопределяет его на этапе «Проверка»: подтверждает интерес выбранных
отделов или оставляет доклад только с карточкой. Пока отбор не завершён, устарел или
требует решения оператора, доклад ждёт. Обработка (processing.py) записывает итог
в talks.card_only; справка выводит такой доклад без сводки.
"""

import sqlite3
from enum import StrEnum
from typing import Any

from rapporteur import db, screening
from rapporteur.contracts.enums import REPORTABLE_TALK_STATUSES, TalkStatus


class Route(StrEnum):
    SUMMARY = "summary"
    CARD = "card"


def decided(view: dict[str, Any]) -> Route | None:
    """Итог отбора по его представлению screening.view(); None — решения ещё нет."""
    match view["state"]:
        case "relevant":
            return Route.SUMMARY
        case "not_relevant":
            return Route.CARD
    return None


def route(conn: sqlite3.Connection, talk_id: str) -> Route | None:
    return decided(screening.view(conn, talk_id))


def reportable(conn: sqlite3.Connection, talk_id: str) -> bool:
    """Доклад входит в справку, когда его обработка соответствует актуальному отбору:
    сводка у интересного отделам доклада, карточка у остальных. Наличие и состояние самой
    сводки проверяет сборка справки (reports.py, report/assemble.py)."""
    row = db.one(conn, "SELECT status, card_only FROM talks WHERE id = ?", talk_id)
    if row is None:
        return False
    match route(conn, talk_id):
        case Route.SUMMARY:
            return not row["card_only"]
        case Route.CARD:
            return bool(row["card_only"]) and TalkStatus(row["status"]) in REPORTABLE_TALK_STATUSES
    return False
