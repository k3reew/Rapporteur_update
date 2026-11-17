"""База SQLite на томе данных (ADR-014).

Схема создаётся при старте приложения, затем шаги _MIGRATIONS доводят её до текущей версии
(PRAGMA user_version). Перед каждым шагом рядом с базой сохраняется копия .before-vN.bak.
Каждая запись идёт в транзакции BEGIN IMMEDIATE: веб-запросы и фоновая обработка работают
в одном процессе и не мешают друг другу.
"""

import datetime as dt
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rapporteur.contracts import Conference, Section, SourceFile, Speaker, Talk

SCHEMA = """
CREATE TABLE IF NOT EXISTS conferences (
    id TEXT PRIMARY KEY,
    title TEXT,
    start_date TEXT,
    end_date TEXT,
    place TEXT,
    organizer TEXT,
    processing_started INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
    id TEXT PRIMARY KEY,
    conference_id TEXT NOT NULL REFERENCES conferences(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    date TEXT,
    position INTEGER NOT NULL
);
-- job: queued → running → done | failed; step — текущий шаг обработки доклада.
CREATE TABLE IF NOT EXISTS talks (
    id TEXT PRIMARY KEY,
    section_id TEXT NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    title TEXT,
    date TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    speakers TEXT NOT NULL DEFAULT '[]',
    import_folder TEXT,
    job TEXT,
    step TEXT,
    error TEXT,
    queued_at TEXT,
    UNIQUE (section_id, import_folder)
);
-- Файл конференции вместе с привязкой к докладу (spec §6.1, проверка 1).
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    conference_id TEXT NOT NULL REFERENCES conferences(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    original_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_ms INTEGER,
    encoding TEXT,
    title_candidates TEXT,
    warning TEXT,
    talk_id TEXT REFERENCES talks(id) ON DELETE SET NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    confidence REAL,
    UNIQUE (conference_id, sha256),
    UNIQUE (conference_id, relative_path)
);
-- Версии сводки доклада вместе с пакетом фрагментов; текущая — с наибольшим номером.
CREATE TABLE IF NOT EXISTS summaries (
    talk_id TEXT NOT NULL REFERENCES talks(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    summary TEXT NOT NULL,
    pack TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (talk_id, number)
);
CREATE TABLE IF NOT EXISTS report_versions (
    id TEXT PRIMARY KEY,
    conference_id TEXT NOT NULL REFERENCES conferences(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    input TEXT NOT NULL,
    variables TEXT NOT NULL,
    files TEXT NOT NULL,
    warnings TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (conference_id, number)
);
-- Настройки, изменяемые из интерфейса: например, выбранная модель (model_choice.py).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS talks_job ON talks (job, queued_at);
CREATE INDEX IF NOT EXISTS files_talk ON files (talk_id);
"""

Row = sqlite3.Row


def _v1(conn: sqlite3.Connection) -> None:
    """Подготовка по материалам, отделы и рекомендации."""
    conn.execute("ALTER TABLE talks ADD COLUMN input_revision INTEGER NOT NULL DEFAULT 0")
    conn.execute("""CREATE TABLE material_links (
        id TEXT PRIMARY KEY,
        file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        talk_id TEXT NOT NULL REFERENCES talks(id) ON DELETE CASCADE,
        start_ms INTEGER, end_ms INTEGER, page_start INTEGER, page_end INTEGER
    )""")
    conn.execute("CREATE INDEX links_talk ON material_links(talk_id)")
    conn.execute("""CREATE TABLE preparations (
        conference_id TEXT PRIMARY KEY REFERENCES conferences(id) ON DELETE CASCADE,
        version INTEGER NOT NULL DEFAULT 1,
        job TEXT NOT NULL, error TEXT, proposal TEXT NOT NULL DEFAULT '[]'
    )""")
    conn.execute("""CREATE TABLE departments (
        id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
        original_name TEXT NOT NULL, sha256 TEXT NOT NULL,
        pages TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(id, version)
    )""")
    conn.execute("""CREATE TABLE recommendations (
        talk_id TEXT PRIMARY KEY REFERENCES talks(id) ON DELETE CASCADE,
        summary_version INTEGER NOT NULL, department_revisions TEXT NOT NULL,
        version INTEGER NOT NULL, items TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE recommendation_jobs (
        talk_id TEXT PRIMARY KEY REFERENCES talks(id) ON DELETE CASCADE,
        job TEXT NOT NULL, error TEXT
    )""")


def _v2(conn: sqlite3.Connection) -> None:
    """Карточка доклада: организации и контакты без привязки к автору (JSON-списки).
    Организация и контакты автора хранятся в talks.speakers."""
    conn.execute("ALTER TABLE talks ADD COLUMN organizations TEXT NOT NULL DEFAULT '[]'")
    conn.execute("ALTER TABLE talks ADD COLUMN contacts TEXT NOT NULL DEFAULT '[]'")


def _v3(conn: sqlite3.Connection) -> None:
    """Сводка или только карточка (interest.py): scope — выбор оператора, card_only —
    итог последней обработки."""
    conn.execute(
        "ALTER TABLE talks ADD COLUMN scope TEXT NOT NULL DEFAULT 'auto' "
        "CHECK (scope IN ('auto', 'summary', 'card'))"
    )
    conn.execute("ALTER TABLE talks ADD COLUMN card_only INTEGER NOT NULL DEFAULT 0")


_MIGRATIONS = (_v1, _v2, _v3)
SCHEMA_VERSION = len(_MIGRATIONS)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Шаги схемы по PRAGMA user_version; перед каждым — копия базы рядом с ней."""
        for number, step in enumerate(_MIGRATIONS, 1):
            if conn.execute("PRAGMA user_version").fetchone()[0] >= number:
                continue
            # SQLite backup also includes committed WAL data.
            with sqlite3.connect(f"{self.path}.before-v{number}.bak") as backup:
                conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
            try:
                step(conn)
                conn.execute(f"PRAGMA user_version = {number}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def connect(self) -> sqlite3.Connection:
        # Соединение живёт в одном запросе, но FastAPI может открыть и закрыть его в разных
        # потоках пула.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """Consistent read snapshot without reserving SQLite's writer lock."""
        conn = self.connect()
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("BEGIN")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def new_id() -> str:
    return str(uuid.uuid4())


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def one(conn: sqlite3.Connection, sql: str, *params: Any) -> Row | None:
    row: Row | None = conn.execute(sql, params).fetchone()
    return row


def conference(row: Row) -> Conference:
    return Conference.model_validate(
        {key: row[key] for key in ("id", "title", "start_date", "end_date", "place", "organizer")}
    )


def section(row: Row) -> Section:
    return Section(
        id=row["id"],
        conference_id=row["conference_id"],
        title=row["title"],
        date=row["date"],
        order=row["position"],
    )


def talk(row: Row) -> Talk:
    return Talk(
        id=row["id"],
        section_id=row["section_id"],
        order=row["position"],
        title=row["title"],
        date=row["date"],
        status=row["status"],
        speakers=[Speaker.model_validate(item) for item in json.loads(row["speakers"])],
        organizations=json.loads(row["organizations"]),
        contacts=json.loads(row["contacts"]),
        card_only=bool(row["card_only"]),
    )


def source_file(row: Row) -> SourceFile:
    return SourceFile.model_validate(
        {key: row[key] for key in SourceFile.model_fields if key != "conference_id"}
        | {"conference_id": row["conference_id"]}
    )


def conference_talks(conn: sqlite3.Connection, conference_id: str) -> list[Row]:
    """Доклады конференции в порядке секций и номеров, с названием секции."""
    return conn.execute(
        "SELECT talks.*, sections.title AS section_title, sections.date AS section_date "
        "FROM talks JOIN sections ON sections.id = talks.section_id "
        "WHERE sections.conference_id = ? ORDER BY sections.position, talks.position",
        (conference_id,),
    ).fetchall()


def current_summaries(conn: sqlite3.Connection, conference_id: str) -> dict[str, Row]:
    """Текущая версия сводки каждого доклада конференции."""
    rows = conn.execute(
        "SELECT summaries.* FROM summaries "
        "JOIN talks ON talks.id = summaries.talk_id "
        "JOIN sections ON sections.id = talks.section_id "
        "WHERE sections.conference_id = ? AND summaries.number = ("
        "  SELECT max(number) FROM summaries AS newer WHERE newer.talk_id = summaries.talk_id)",
        (conference_id,),
    ).fetchall()
    return {row["talk_id"]: row for row in rows}
