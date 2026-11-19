"""Versioned preliminary department interest, before facts and summaries are generated."""

import json
import logging
import sqlite3
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rapporteur import db, departments, model_choice
from rapporteur.contracts import ModelProfile, TalkFragmentPack
from rapporteur.errors import NotFoundError, StageError, UserError
from rapporteur.llm.client import LlmClient
from rapporteur.llm.tasks import json_task
from rapporteur.storage import Storage, cache_key
from rapporteur.summarize.quotes import QuoteConfig, match_quote

log = logging.getLogger("rapporteur.screening")
VERSION = "1.0.0"


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=10, max_length=240)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["relevant", "not_relevant", "uncertain"]
    reason: str = Field(min_length=1, max_length=600)
    evidence: list[Quote] = Field(max_length=3)
    paragraphs: list[Quote] = Field(max_length=3)

    @model_validator(mode="after")
    def supported_interest(self) -> "Decision":
        if self.status == "relevant" and (not self.evidence or not self.paragraphs):
            raise ValueError("Для интереса нужны цитаты доклада и положения")
        return self


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    department_ids: list[str] = Field(max_length=1000)


def _latest(conn: sqlite3.Connection, talk_id: str) -> db.Row | None:
    return db.one(
        conn, "SELECT * FROM talk_screenings WHERE talk_id=? ORDER BY version DESC LIMIT 1", talk_id
    )


def _snapshot(conn: sqlite3.Connection, talk_id: str) -> tuple[int, str, str]:
    talk = db.one(conn, "SELECT input_revision FROM talks WHERE id=?", talk_id)
    if talk is None:
        raise NotFoundError("Доклад не найден")
    return (
        talk["input_revision"],
        db.dumps(departments.revisions(conn)),
        model_choice.active_profile(conn).model_dump_json(),
    )


def view(conn: sqlite3.Connection, talk_id: str) -> dict[str, Any]:
    snapshot = _snapshot(conn, talk_id)
    row = _latest(conn, talk_id)
    if row is None:
        return {
            "version": 0,
            "state": "not_started",
            "eligible": False,
            "stale": False,
            "items": [],
            "department_ids": [],
            "error": None,
            "manual": False,
        }
    stale = snapshot != (row["input_revision"], row["department_revisions"], row["profile"])
    items = json.loads(row["result"])
    if row["job"] == "failed" and not items:
        items = [
            {
                "department_id": dep["id"],
                "department_name": dep["name"],
                "department_version": dep["version"],
                "status": "uncertain",
                "reason": "Автоматическая оценка не завершена",
                "evidence": [],
                "paragraphs": [],
            }
            for dep in departments.latest(conn)
        ]
    selected = [item["department_id"] for item in items if item["status"] == "relevant"]
    state = (
        "relevant"
        if selected
        else "uncertain"
        if any(i["status"] == "uncertain" for i in items)
        else "not_relevant"
    )
    if row["job"] != "done":
        state = "uncertain" if row["job"] == "failed" else row["job"]
    if stale:
        state = "stale"
    return {
        "version": row["version"],
        "state": state,
        "eligible": state == "relevant",
        "stale": stale,
        "items": items,
        "department_ids": selected,
        "error": row["error"],
        "manual": bool(row["manual"]),
    }


def queue(conn: sqlite3.Connection, talk_id: str, *, force: bool = False) -> None:
    departments.require_regulations(conn)
    current = view(conn, talk_id)
    # Приостановленный после перезапуска отбор продолжается новой версией (processing.py).
    paused = current["state"] == "paused"
    if (
        current["version"]
        and not current["stale"]
        and not current["error"]
        and not paused
        and not force
    ):
        return
    revision, deps, profile = _snapshot(conn, talk_id)
    conn.execute(
        "INSERT INTO "
        "talk_screenings(talk_id,version,input_revision,"
        "department_revisions,profile,job,created_at) VALUES "
        "(?,?,?,?,?,'queued',?)",
        (talk_id, current["version"] + 1, revision, deps, profile, db.now()),
    )


def review(conn: sqlite3.Connection, talk_id: str, data: Review) -> None:
    current = view(conn, talk_id)
    if (
        current["version"] != data.version
        or current["stale"]
        or current["state"] in ("queued", "running")
    ):
        raise UserError("Отбор изменился или устарел. Обновите данные.", 409)
    deps = departments.require_regulations(conn)
    selected = set(data.department_ids)
    if not selected <= {dep["id"] for dep in deps}:
        raise UserError("Выбран неизвестный отдел")
    items = [
        {
            "department_id": dep["id"],
            "department_version": dep["version"],
            "department_name": dep["name"],
            "status": "relevant" if dep["id"] in selected else "not_relevant",
            "reason": "Интерес подтверждён оператором"
            if dep["id"] in selected
            else "Исключён оператором",
            "evidence": [],
            "paragraphs": [],
        }
        for dep in deps
    ]
    revision, revisions, profile = _snapshot(conn, talk_id)
    conn.execute(
        "INSERT INTO "
        "talk_screenings(talk_id,version,input_revision,"
        "department_revisions,profile,job,result,manual,created_at) "
        "VALUES (?,?,?,?,?,'done',?,1,?)",
        (talk_id, data.version + 1, revision, revisions, profile, db.dumps(items), db.now()),
    )


def _chunks(items: list[dict[str, str]], budget: int) -> Iterator[list[dict[str, str]]]:
    batch: list[dict[str, str]] = []
    size = 0
    for item in items:
        for start in range(0, len(item["text"]), max(1, budget // 2)):
            part = {"label": item["label"], "text": item["text"][start : start + budget // 2]}
            length = len(part["text"]) + 100
            if batch and (
                size + length > budget or any(p["label"] == part["label"] for p in batch)
            ):
                yield batch
                batch, size = [], 0
            batch.append(part)
            size += length
    if batch:
        yield batch


def _classify(
    client: LlmClient,
    profile: ModelProfile,
    sources: list[dict[str, str]],
    regulations: list[dict[str, str]],
    task: str,
) -> Decision:
    source_text = {item["label"]: item["text"] for item in sources}
    regulation_text = {item["label"]: item["text"] for item in regulations}
    schema = Decision.model_json_schema()
    for name, texts in (("evidence", source_text), ("paragraphs", regulation_text)):
        item = Quote.model_json_schema()
        item["properties"]["label"]["enum"] = list(texts)
        schema["properties"][name]["items"] = item

    def check(value: Decision) -> list[str]:
        problems = []
        for quotes, texts in ((value.evidence, source_text), (value.paragraphs, regulation_text)):
            for quote in quotes:
                if (
                    quote.label not in texts
                    or match_quote(quote.quote, texts[quote.label], QuoteConfig()).rejection
                    is not None
                ):
                    problems.append("Не найдена цитата: " + quote.label)
        return problems

    result = json_task(
        client,
        profile,
        "Определи конкретную связь темы доклада с обязанностями отдела из положения. "
        "Совпадения общих слов недостаточно. relevant требует цитат обеих сторон. "
        "При неоднозначности верни uncertain, при отсутствии связи not_relevant. "
        "Это предварительный отбор, не итоговая рекомендация. Пояснение по-русски.",
        db.dumps({"sources": sources, "regulations": regulations}),
        Decision,
        task=task,
        schema=schema,
        check=check,
    )
    if result.status == "relevant" and all(item.label.startswith("V") for item in result.evidence):
        return Decision(
            status="uncertain",
            reason="Связь основана только на модельном описании изображения.",
            evidence=result.evidence,
            paragraphs=result.paragraphs,
        )
    return result


def run_once(
    database: db.Database,
    storage: Storage,
    factory: Callable[[ModelProfile], LlmClient],
    pack_factory: Callable[[str], TalkFragmentPack],
) -> str | None:
    with database.tx() as conn:
        row = db.one(
            conn,
            "SELECT * FROM talk_screenings s WHERE job='queued' AND version=(SELECT "
            "max(version) FROM talk_screenings x WHERE x.talk_id=s.talk_id) "
            "ORDER BY created_at LIMIT 1",
        )
        if row is None:
            return None
        talk_id: str = row["talk_id"]
        version = row["version"]
        conn.execute(
            "UPDATE talk_screenings SET job='running' WHERE talk_id=? AND version=?",
            (talk_id, version),
        )
        log.info("Отбор по интересу отделов: доклад %s, версия %s", talk_id, version)
        profile = ModelProfile.model_validate_json(row["profile"])
    try:
        with database.read() as conn:
            deps = departments.require_regulations(conn)
        pack = pack_factory(talk_id)
        client = factory(profile)
        budget = max(
            1000,
            min(
                10000,
                (
                    profile.generation.context_tokens
                    - min(profile.generation.max_tokens, 6000)
                    - 4000
                ),
            ),
        )
        source_batches = list(
            _chunks([{"label": f.label, "text": f.text} for f in pack.fragments], budget)
        )
        results = []
        for dep in deps:
            decisions = []
            regulation_batches = list(
                _chunks(
                    [
                        {"label": key, "text": text}
                        for key, text in departments.paragraphs(dep).items()
                        if text.strip()
                    ],
                    min(4000, budget),
                )
            )
            for sources in source_batches:
                for regulations in regulation_batches:
                    digest = cache_key(
                        VERSION,
                        profile.model_dump(),
                        sources,
                        dep["id"],
                        dep["version"],
                        regulations,
                    )
                    key = f"cache/screening/{digest}.json"
                    try:
                        found = storage.cached(
                            key,
                            Decision,
                            partial(
                                _classify,
                                client,
                                profile,
                                sources,
                                regulations,
                                f"Отбор {talk_id}, отдел {dep['name']}",
                            ),
                        )
                    except StageError as error:
                        found = Decision(
                            status="uncertain",
                            reason=error.message[:600],
                            evidence=[],
                            paragraphs=[],
                        )
                    decisions.append(found)
                    if found.status == "relevant":
                        break
                if decisions and decisions[-1].status == "relevant":
                    break
            priority = {"relevant": 0, "uncertain": 1, "not_relevant": 2}
            chosen = (
                min(decisions, key=lambda d: priority[d.status])
                if decisions
                else Decision(
                    status="uncertain",
                    reason="Недостаточно извлечённых сведений",
                    evidence=[],
                    paragraphs=[],
                )
            )
            results.append(
                {
                    "department_id": dep["id"],
                    "department_version": dep["version"],
                    "department_name": dep["name"],
                    **chosen.model_dump(),
                }
            )
        with database.tx() as conn:
            current = _latest(conn, talk_id)
            if current is None or current["version"] != version:
                return talk_id
            if _snapshot(conn, talk_id) != (
                row["input_revision"],
                row["department_revisions"],
                row["profile"],
            ):
                queue(conn, talk_id, force=True)
            conn.execute(
                "UPDATE talk_screenings SET job='done',result=? WHERE talk_id=? AND version=?",
                (db.dumps(results), talk_id, version),
            )
    except Exception as error:
        log.exception("Отбор доклада %s", talk_id)
        with database.tx() as conn:
            conn.execute(
                "UPDATE talk_screenings SET job='failed',error=? WHERE talk_id=? AND version=?",
                (
                    (
                        f"{error.code.value}: {error.message}"
                        if isinstance(error, StageError)
                        else str(error)
                    )[:2000],
                    talk_id,
                    version,
                ),
            )
    return talk_id
