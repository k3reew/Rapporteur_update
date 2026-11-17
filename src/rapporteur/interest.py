"""Нужна ли докладу сводка: точка стыковки с классификацией интереса отделов.

Доклад, интересный хотя бы одному отделу, получает краткую сводку. Остальным достаточно
карточки: темы, авторов, организаций и контактов. Решение принимает классификация отделов
(classify), оператор может его переопределить полем talks.scope: «сводка» или «только
карточка». Обработка (processing.py) спрашивает needs_summary() после сборки пакета
фрагментов и записывает итог в talks.card_only; справка выводит такой доклад без сводки.
"""

import sqlite3
from enum import StrEnum

from rapporteur import db
from rapporteur.contracts import TalkFragmentPack


class Scope(StrEnum):
    """Выбор оператора: auto — по классификации, summary — сводка, card — только карточка."""

    AUTO = "auto"
    SUMMARY = "summary"
    CARD = "card"


def classify(conn: sqlite3.Connection, talk_id: str, pack: TalkFragmentPack) -> bool | None:
    """Интересен ли доклад хотя бы одному отделу.

    Здесь подключается классификация отделов. None — решения нет: доклад обрабатывается
    целиком, как до классификации. Вызов идёт в потоке обработки, вне HTTP-запросов.
    """
    return None


def needs_summary(conn: sqlite3.Connection, talk_id: str, pack: TalkFragmentPack) -> bool:
    row = db.one(conn, "SELECT scope FROM talks WHERE id = ?", talk_id)
    scope = Scope(row["scope"]) if row else Scope.AUTO
    if scope is not Scope.AUTO:
        return scope is Scope.SUMMARY
    return classify(conn, talk_id, pack) is not False
