"""Фоновая подготовка, отбор и обработка независимых докладов с очередью в SQLite."""

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

from rapporteur import (
    db,
    departments,
    interest,
    materials,
    model_choice,
    preparation,
    report_jobs,
    screening,
)
from rapporteur.contracts import (
    FactsArtifact,
    ModelProfile,
    NotesArtifact,
    SlidesArtifact,
    Summary,
    TalkFragmentPack,
    can_transition,
)
from rapporteur.contracts.enums import FileKind, TalkStatus
from rapporteur.errors import FailureCode, StageError, UserError
from rapporteur.ingest import notes as notes_parser
from rapporteur.ingest import slides as slides_parser
from rapporteur.ingest.limits import NotesConfig, PresentationConfig
from rapporteur.llm.client import LlmClient
from rapporteur.storage import Storage, blob_key, cache_key
from rapporteur.summarize import facts as facts_step
from rapporteur.summarize import summary as summary_step
from rapporteur.summarize.checks import load_checks
from rapporteur.summarize.pack import PackSource, build_pack
from rapporteur.summarize.quotes import QuoteConfig
from rapporteur.summarize.verify import verify_facts

log = logging.getLogger("rapporteur.processing")

# Доклады с материалами, которые обработка берёт в работу при запуске.
READY_STATUSES = (TalkStatus.MATCHED.value, TalkStatus.STALE.value)
_RECORDINGS = {FileKind.AUDIO.value, FileKind.VIDEO.value}

ClientFactory = Callable[[ModelProfile], LlmClient]


def enqueue(conn: sqlite3.Connection, talk_ids: list[str]) -> None:
    """Повторная постановка во время обработки допустима: доклад пройдёт цепочку ещё раз.
    Обработка ждёт итога отбора: сводки для интересного отделам доклада или карточки для
    остальных (interest.py)."""
    departments.require_regulations(conn)
    for talk_id in talk_ids:
        screening.queue(conn, talk_id)
        decided = interest.processing_route(conn, talk_id) is not None
        conn.execute(
            "UPDATE talks SET job=?, step=?, error=NULL, queued_at=? WHERE id=?",
            ("queued" if decided else None, None if decided else "screening", db.now(), talk_id),
        )


def enqueue_reviewed(
    conn: sqlite3.Connection, talk_id: str, previous: interest.Route | None
) -> None:
    current = interest.processing_route(conn, talk_id)
    row = db.one(conn, "SELECT job,card_only FROM talks WHERE id=?", talk_id)
    if row and interest.automatic(conn, talk_id) and current is previous is interest.Route.SUMMARY:
        if row["job"] in ("queued", "running") or (row["job"] == "done" and not row["card_only"]):
            return
    enqueue(conn, [talk_id])


def start_conference(conn: sqlite3.Connection, conference_id: str) -> None:
    departments.require_regulations(conn)
    conn.execute("UPDATE conferences SET processing_started=1 WHERE id=?", (conference_id,))
    enqueue_ready(conn, conference_id)


def enqueue_ready(conn: sqlite3.Connection, conference_id: str) -> int:
    """Ставит доклады с привязанными материалами, которые ещё не обработаны или устарели.
    Доклад в работе ставится заново: его материалы могли измениться."""
    talks = (
        preparation.screening_talks(conn, conference_id)
        if preparation.operator(conn, conference_id)
        else db.conference_talks(conn, conference_id)
    )
    rows = [
        row["id"] for row in talks if row["status"] in READY_STATUSES and row["job"] != "queued"
    ]
    enqueue(conn, rows)
    return len(rows)


def slides_cache_key(sha256: str) -> str:
    return cache_key("slides", slides_parser.VERSION, sha256, PresentationConfig().model_dump())


def slide_image_key(sha256: str, slide_no: int) -> str:
    return f"slides/{slides_cache_key(sha256)}/slide-{slide_no:03}.png"


# Задания всех очередей, которые после перезапуска ждут команды оператора.
PAUSED = "paused"
_PAUSE = (
    ("подготовка", "UPDATE preparations SET job='paused' WHERE job IN ('queued','running')"),
    ("отбор", "UPDATE talk_screenings SET job='paused' WHERE job IN ('queued','running')"),
    ("доклады", "UPDATE talks SET job='paused', step=NULL WHERE job IN ('queued','running')"),
    (
        "рекомендации",
        "UPDATE recommendation_jobs SET job='paused' WHERE job IN ('queued','running')",
    ),
    (
        "справки",
        "UPDATE report_jobs SET status='paused', step='paused' "
        "WHERE status IN ('queued','running')",
    ),
)


def pause_unfinished(conn: sqlite3.Connection) -> dict[str, int]:
    """Возобновляет задания запуска нового сценария. Старые установки требуют команды
    оператора, чтобы не запускать отложенные работы при переносе старого хранилища."""
    conn.execute("UPDATE talks SET worker_active=0 WHERE worker_active<>0")
    paused = {name: conn.execute(sql).rowcount for name, sql in _PAUSE}
    operator_conferences = "SELECT conference_id FROM preparations WHERE workflow='operator'"
    operator_talks = (
        "SELECT t.id FROM talks t JOIN sections s ON s.id=t.section_id "
        f"WHERE s.conference_id IN ({operator_conferences})"
    )
    for name, sql in (
        (
            "подготовка",
            "UPDATE preparations SET job='queued' WHERE job='paused' AND workflow='operator'",
        ),
        (
            "отбор",
            "UPDATE talk_screenings SET job='queued' WHERE job='paused' "
            f"AND talk_id IN ({operator_talks})",
        ),
        (
            "доклады",
            f"UPDATE talks SET job='queued' WHERE job='paused' AND id IN ({operator_talks})",
        ),
        (
            "справки",
            f"UPDATE report_jobs SET status='queued',step='queued' WHERE status='paused' "
            f"AND conference_id IN ({operator_conferences})",
        ),
    ):
        resumed = conn.execute(sql).rowcount
        if resumed:
            paused[name] = max(0, paused[name] - resumed)
            log.info("Возобновлены задания (%s): %d", name, resumed)
    return paused


@dataclass(frozen=True)
class _Talk:
    id: str
    conference_id: str
    files: list[db.Row]
    revision: int
    route: interest.Route | None = None
    basis: str = ""


class Processor:
    def __init__(self, database: db.Database, storage: Storage, client: ClientFactory) -> None:
        self.database = database
        self.storage = storage
        self.client = client
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.slots = 2 if os.environ.get("RAPPORTEUR_INFERENCE_SLOTS") == "2" else 1

    def start(self) -> None:
        with self.database.tx() as conn:
            paused = pause_unfinished(conn)
        if any(paused.values()):
            log.warning(
                "После перезапуска приостановлены незавершённые задания: %s. "
                "Модель не работает, пока оператор не продолжит их в интерфейсе.",
                ", ".join(f"{name} — {count}" for name, count in paused.items() if count),
            )
        else:
            log.info("Обработчик очереди готов")
        self._thread = threading.Thread(target=self._loop, name="processing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        with ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="talk") as pool:
            while not self._stop.is_set():
                try:
                    worked = self.run_once(pool if self.slots > 1 else None)
                except Exception:
                    log.exception("Сбой цикла обработки")
                    worked = False
                if not worked:
                    self._wake.wait(timeout=5)
                    self._wake.clear()

    def run_once(self, pool: ThreadPoolExecutor | None = None) -> bool:
        """Обрабатывает следующий доклад из очереди; False, если очередь пуста."""
        if report_jobs.run_once(self.database, self.storage):
            return True
        if self._prepare_once(pool):
            return True
        if pool is None:
            worked = self._talk_once()
        else:
            work = [pool.submit(self._talk_once) for _ in range(self.slots)]
            results = [task.result() for task in work]
            worked = any(results)
        return worked or departments.run_once(self.database, self.client)

    def _talk_once(self) -> bool:
        screened = screening.run_once(self.database, self.storage, self.client, self._screen_pack)
        if screened is not None:
            with self.database.tx() as conn:
                row = db.one(conn, "SELECT job FROM talks WHERE id=?", screened)
                if row and interest.processing_route(conn, screened) is not None:
                    if row["job"] not in ("queued", "running", "done"):
                        enqueue(conn, [screened])
                elif row and (error := screening.view(conn, screened)["error"]):
                    conn.execute(
                        "UPDATE talks SET job='failed',step='screening',error=? WHERE id=?",
                        (error, screened),
                    )
        talk = self._take()
        if talk is None:
            return screened is not None
        log.info("Обработка доклада %s", talk.id)
        try:
            if talk.route is interest.Route.CARD:
                summary, pack = None, None
            else:
                summary, pack = self._process(talk)
        except StageError as error:
            log.warning("Доклад %s: %s", talk.id, error.code.value)
            self._finish(
                talk.id,
                "failed",
                f"{error.code.value}: {error.message}",
                talk.revision,
                talk.basis,
                talk.route,
            )
        except Exception:
            log.exception("Доклад %s: внутренняя ошибка", talk.id)
            self._finish(
                talk.id,
                "failed",
                "internal_error: внутренняя ошибка, см. журнал",
                talk.revision,
                talk.basis,
                talk.route,
            )
        else:
            if summary is None:
                self._save_card(talk.id, talk.revision, talk.basis, talk.route)
            else:
                assert pack is not None
                self._save(talk.id, summary, pack, talk.revision, talk.basis, talk.route)
        finally:
            with self.database.tx() as conn:
                conn.execute("UPDATE talks SET worker_active=0 WHERE id=?", (talk.id,))
        return True

    def _screen_pack(self, talk_id: str) -> TalkFragmentPack:
        with self.database.read() as conn:
            talk = self._load_talk(conn, talk_id)
        return self._pack(talk)

    def _load_talk(self, conn: sqlite3.Connection, talk_id: str) -> _Talk:
        row = db.one(
            conn,
            "SELECT talks.id, talks.input_revision, sections.conference_id "
            "FROM talks JOIN sections ON sections.id=talks.section_id WHERE talks.id=?",
            talk_id,
        )
        if row is None:
            raise ValueError("Доклад не найден")
        files = conn.execute(
            "SELECT files.*, NULL AS start_ms, NULL AS end_ms, NULL AS "
            "page_start, NULL AS page_end "
            "FROM files WHERE talk_id = ? AND confirmed = 1 "
            "AND NOT EXISTS (SELECT 1 FROM material_links WHERE file_id=files.id) "
            "UNION ALL SELECT files.*, material_links.start_ms, material_links.end_ms, "
            "material_links.page_start, material_links.page_end FROM material_links "
            "JOIN files ON files.id=material_links.file_id WHERE material_links.talk_id=? "
            "ORDER BY relative_path",
            (talk_id, talk_id),
        ).fetchall()
        return _Talk(row["id"], row["conference_id"], files, row["input_revision"])

    def _take(self) -> _Talk | None:
        with self.database.tx() as conn:
            rows = conn.execute(
                "SELECT id FROM talks WHERE job='queued' AND worker_active=0 ORDER BY queued_at"
            ).fetchall()
            for row in rows:
                route = interest.processing_route(conn, row["id"])
                if route is None:
                    try:
                        enqueue(conn, [row["id"]])
                    except UserError as error:
                        conn.execute(
                            "UPDATE talks SET job='failed',error=? WHERE id=?",
                            (str(error), row["id"]),
                        )
                    continue
                conn.execute(
                    "UPDATE talks SET job='running',step='parse',error=NULL,worker_active=1 "
                    "WHERE id=?",
                    (row["id"],),
                )
                return replace(
                    self._load_talk(conn, row["id"]), route=route, basis=self._basis(conn)
                )
        return None

    def _step(self, talk_id: str, step: str) -> None:
        with self.database.tx() as conn:
            conn.execute(
                "UPDATE talks SET step = ? WHERE id = ? AND job = 'running'", (step, talk_id)
            )

    def _finish(
        self,
        talk_id: str,
        job: str | None,
        error: str,
        revision: int,
        basis: str,
        route: interest.Route | None,
    ) -> None:
        with self.database.tx() as conn:
            if self._current(conn, talk_id, revision, basis, route) is None:
                return
            conn.execute(
                "UPDATE talks SET job = ?, step = NULL, error = ? WHERE id = ? AND job = 'running'",
                (job, error, talk_id),
            )

    def _process(self, talk: _Talk) -> tuple[Summary | None, TalkFragmentPack]:
        """Сводка доклада; None — докладу достаточно карточки (interest.py)."""
        pack = self._pack(talk)
        # Модель — выбранная на странице «Управление» или профиль по умолчанию.
        with self.database.read() as conn:
            profile = model_choice.active_profile(conn)
            if talk.route is interest.Route.CARD:
                return None, pack

        self._step(talk.id, "facts")
        facts_config = facts_step.FactsConfig(profile=profile)
        facts_key = cache_key(
            "facts", facts_step.VERSION, pack.model_dump(mode="json"), facts_config.model_dump()
        )
        facts = self.storage.cached(
            f"cache/facts/{facts_key}.json",
            FactsArtifact,
            lambda: facts_step.extract_facts(
                self.client(profile), pack, facts_config, self.storage
            ),
        )

        self._step(talk.id, "verify")
        claims = verify_facts(facts.facts, pack, QuoteConfig(), id_seed=facts_key)

        self._step(talk.id, "summary")
        started = time.monotonic()
        summary_config = summary_step.SummaryConfig(profile=profile, checks=load_checks())
        summary_key = cache_key(
            "summary", summary_step.VERSION, facts_key, summary_config.model_dump()
        )

        def write() -> Summary:
            written = summary_step.write_summary(
                self.client(profile), summary_config, facts.facts.title, claims, pack
            )
            return summary_step.make_summary(
                summary_key,
                pack,
                facts,
                claims,
                written,
                profile=profile.name,
                prompt_version=profile.prompt_version,
            )

        summary = self.storage.cached(f"cache/summary/{summary_key}.json", Summary, write)
        log.info("stage=summary seconds=%.3f", time.monotonic() - started)
        return summary, pack

    def _pack(self, talk: _Talk) -> TalkFragmentPack:
        presentations: list[PackSource] = []
        notes: list[PackSource] = []
        transcripts: list[PackSource] = []
        asr_skipped = False
        with self.database.read() as conn:
            profile = model_choice.active_profile(conn)
        for file in talk.files:
            source = UUID(file["id"])
            key = preparation.prepared_key(file, profile.model_dump_json())
            if self.storage.exists(key):
                fragments = preparation.Prepared.model_validate_json(
                    self.storage.get(key)
                ).fragments
            elif file["kind"] == FileKind.PRESENTATION.value:
                fragments = self._slides(file["sha256"]).fragments
            elif file["kind"] == FileKind.NOTES.value:
                fragments = self._notes(file["sha256"]).fragments
            else:
                prepared = preparation.extract(file, self.storage, self.client(profile), profile)
                fragments = prepared.fragments
                asr_skipped |= preparation.ASR_DISABLED in prepared.warnings
            link = preparation.Link(
                file_id=file["id"],
                start_ms=file["start_ms"],
                end_ms=file["end_ms"],
                page_start=file["page_start"],
                page_end=file["page_end"],
            )
            fragments = preparation.slice_fragments(fragments, link)
            destination = (
                transcripts
                if file["kind"] in _RECORDINGS
                else notes
                if file["kind"] == FileKind.NOTES
                else presentations
            )
            destination.append(PackSource(source, fragments))
        pack = build_pack(
            UUID(talk.conference_id), UUID(talk.id), presentations, notes, transcripts
        )
        if not pack.fragments:
            raise StageError(
                FailureCode.NO_SOURCES,
                "В материалах доклада нет текста или читаемых изображений"
                + ("; распознавание речи выключено, запись не расшифрована" if asr_skipped else ""),
            )
        if asr_skipped:
            log.warning("Доклад %s: запись пропущена, распознавание речи выключено", talk.id)
        return pack

    def _prepare_once(self, pool: ThreadPoolExecutor | None = None) -> bool:
        with self.database.tx() as conn:
            row = db.one(conn, "SELECT * FROM preparations WHERE job='queued' LIMIT 1")
            if row is None:
                return False
            conference_id, version = row["conference_id"], row["version"]
            automatic = bool(row["auto_start"])
            operator = row["workflow"] == "operator"
            context_ids = set(json.loads(row["context_file_ids"])) if operator else None
            prior = [
                preparation.ProposedTalk.model_validate(item)
                for item in json.loads(row["proposal"])
                if operator and item.get("talk_id")
            ]
            try:
                departments.require_regulations(conn)
            except UserError as error:
                conn.execute(
                    "UPDATE preparations SET job='failed',error=? "
                    "WHERE conference_id=? AND version=?",
                    (error.message, conference_id, version),
                )
                return True
            conn.execute(
                "UPDATE preparations SET job='running', error=NULL WHERE conference_id=?",
                (conference_id,),
            )
            log.info("Подготовка конференции %s, версия %s", conference_id, version)
            files = conn.execute(
                "SELECT * FROM files WHERE conference_id=? ORDER BY relative_path", (conference_id,)
            ).fetchall()
            if operator:
                files = [f for f in files if f["kind"] not in _RECORDINGS]
            existing = materials.review(conn, conference_id)
            profile = model_choice.active_profile(conn)
            snapshot = preparation.input_snapshot(conn, conference_id) if automatic else None
            conference = materials.require_conference(conn, conference_id)
            existing_registry = bool(
                conference["processing_started"] or any(f["confirmed"] for f in files)
            )
        try:
            client = self.client(profile)

            def extract(file: db.Row) -> preparation.Prepared:
                try:
                    return preparation.extract(file, self.storage, client, profile)
                except StageError as error:
                    if not automatic or error.code.value.startswith("llm_"):
                        raise
                    return preparation.Prepared(fragments=[], warnings=[error.message])

            prepared = map(extract, files) if pool is None else pool.map(extract, files)
            extracted = {file["id"]: value for file, value in zip(files, prepared, strict=True)}
            usable = [f for f in files if extracted[f["id"]].fragments] if automatic else files
            if automatic:
                ids = {f["id"] for f in usable}
                existing = existing.model_copy(
                    update={
                        "files": [f for f in existing.files if str(f.source.id) in ids],
                    }
                )
            proposal = preparation.propose(
                usable,
                extracted,
                existing,
                client,
                profile,
                self.storage,
                context_ids=context_ids,
                previous_proposal=prior,
            )
        except Exception as error:
            log.exception("Подготовка конференции %s", conference_id)
            with self.database.tx() as conn:
                conn.execute(
                    "UPDATE preparations SET job='failed', error=? WHERE "
                    "conference_id=? AND version=?",
                    (str(error)[:1000], conference_id, version),
                )
        else:
            with self.database.tx() as conn:
                current = db.one(
                    conn,
                    "SELECT job FROM preparations WHERE conference_id=? AND version=?",
                    conference_id,
                    version,
                )
                if current is None or current["job"] != "running":
                    return True
                if automatic and snapshot != preparation.input_snapshot(conn, conference_id):
                    conn.execute(
                        "UPDATE preparations SET job='failed',error=? WHERE conference_id=?",
                        (
                            "Материалы изменились во время анализа. Запустите анализ заново.",
                            conference_id,
                        ),
                    )
                    return True
                reasons = (
                    preparation.review_reasons(
                        files,
                        extracted,
                        proposal,
                        existing_registry=existing_registry,
                        context_ids=context_ids,
                    )
                    if automatic
                    else []
                )
                conn.execute(
                    "UPDATE preparations SET job='done',proposal=?,input_snapshot=?,"
                    "review_reasons=?,context_file_ids=? WHERE conference_id=? AND version=?",
                    (
                        db.dumps([p.model_dump(mode="json") for p in proposal]),
                        snapshot,
                        db.dumps(reasons),
                        db.dumps(sorted(context_ids or ())),
                        conference_id,
                        version,
                    ),
                )
                if automatic and not operator and not reasons:
                    conn.execute("SAVEPOINT apply_proposal")
                    try:
                        preparation.apply(
                            conn,
                            conference_id,
                            preparation.ProposalWrite(version=version, talks=proposal),
                            self.storage,
                        )
                        start_conference(conn, conference_id)
                    except (ValueError, UserError) as error:
                        conn.execute("ROLLBACK TO apply_proposal")
                        conn.execute(
                            "UPDATE preparations SET review_reasons=? WHERE conference_id=?",
                            (db.dumps([str(error)]), conference_id),
                        )
                    finally:
                        conn.execute("RELEASE apply_proposal")
        return True

    def _slides(self, sha: str) -> SlidesArtifact:
        key = slides_cache_key(sha)

        def parse() -> SlidesArtifact:
            with tempfile.TemporaryDirectory(prefix="slides-") as work:
                data = self.storage.get(blob_key(sha))
                return slides_parser.parse_slides(
                    data, sha, self.storage, f"slides/{key}", Path(work)
                )

        return self.storage.cached(f"cache/slides/{key}.json", SlidesArtifact, parse)

    def _notes(self, sha: str) -> NotesArtifact:
        config = NotesConfig()
        key = cache_key("notes", notes_parser.VERSION, sha, config.model_dump())
        return self.storage.cached(
            f"cache/notes/{key}.json",
            NotesArtifact,
            lambda: notes_parser.parse_notes(self.storage.get(blob_key(sha)), sha, config),
        )

    @staticmethod
    def _basis(conn: sqlite3.Connection) -> str:
        return cache_key(
            departments.revisions(conn), model_choice.active_profile(conn).model_dump()
        )

    def _current(
        self,
        conn: sqlite3.Connection,
        talk_id: str,
        revision: int,
        basis: str = "",
        route: interest.Route | None = None,
    ) -> db.Row | None:
        """Доклад, если результат обработки ещё к нему относится; иначе — повтор очереди."""
        talk = db.one(conn, "SELECT status, input_revision, job FROM talks WHERE id = ?", talk_id)
        if talk is None or talk["job"] != "running":
            return None  # Доклад удалили или поставили заново, пока шла обработка.
        if talk["input_revision"] != revision:
            job = "queued" if talk["status"] not in ("insufficient_data", "not_held") else None
            conn.execute("UPDATE talks SET job=?, step=NULL WHERE id=?", (job, talk_id))
            return None
        if basis and (
            basis != self._basis(conn) or route is not interest.processing_route(conn, talk_id)
        ):
            try:
                enqueue(conn, [talk_id])
            except UserError as error:
                conn.execute(
                    "UPDATE talks SET job='failed',error=? WHERE id=?", (error.message, talk_id)
                )
            return None
        return talk

    def _save_card(
        self,
        talk_id: str,
        revision: int,
        basis: str = "",
        route: interest.Route | None = None,
    ) -> None:
        """Доклад без сводки: карточка подтверждена на этапе «Доклады», в справку он идёт
        темой, авторами, организациями и контактами."""
        with self.database.tx() as conn:
            talk = self._current(conn, talk_id, revision, basis, route)
            if talk is None:
                return
            if can_transition(TalkStatus(talk["status"]), TalkStatus.PROCESSED):
                conn.execute(
                    "UPDATE talks SET status = ? WHERE id = ?",
                    (TalkStatus.PROCESSED.value, talk_id),
                )
            conn.execute(
                "UPDATE talks SET card_only = 1, job = 'done', step = NULL, error = NULL "
                "WHERE id = ?",
                (talk_id,),
            )

    def _save(
        self,
        talk_id: str,
        summary: Summary,
        pack: TalkFragmentPack,
        revision: int,
        basis: str = "",
        route: interest.Route | None = None,
    ) -> None:
        """Новая машинная сводка становится очередной версией и требует проверки человеком.
        Тот же результат из кэша повторно не записывается."""
        with self.database.tx() as conn:
            talk = self._current(conn, talk_id, revision, basis, route)
            if talk is None:
                return
            known = db.one(
                conn,
                "SELECT 1 FROM summaries WHERE talk_id=? AND json_extract(summary, '$.id')=?",
                talk_id,
                str(summary.id),
            )
            latest = db.one(
                conn,
                "SELECT pack,summary FROM summaries WHERE talk_id=? ORDER BY number DESC LIMIT 1",
                talk_id,
            )
            if (
                known is None
                or latest is None
                or latest["pack"] != pack.model_dump_json()
                or json.loads(latest["summary"])["status"] == "stale"
            ):
                number = conn.execute(
                    "SELECT coalesce(max(number), 0) + 1 FROM summaries WHERE talk_id = ?",
                    (talk_id,),
                ).fetchone()[0]
                stored = summary.model_copy(update={"version": number})
                conn.execute(
                    "INSERT INTO summaries (talk_id, number, summary, pack, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        talk_id,
                        number,
                        stored.model_dump_json(),
                        pack.model_dump_json(),
                        stored.created_at.isoformat(),
                    ),
                )
            status = TalkStatus(talk["status"])
            if (known is None or status is not TalkStatus.REVIEWED) and can_transition(
                status, TalkStatus.PROCESSED
            ):
                conn.execute(
                    "UPDATE talks SET status = ? WHERE id = ?",
                    (TalkStatus.PROCESSED.value, talk_id),
                )
            conn.execute(
                "UPDATE talks SET card_only = 0, job = 'done', step = NULL, error = NULL "
                "WHERE id = ? AND job = 'running'",
                (talk_id,),
            )
