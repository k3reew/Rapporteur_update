"""Versioned preliminary department interest, before facts and summaries are generated.

Модель для каждого отдела отвечает коротко: уверенность в процентах, что доклад интересен
отделу (interest), одно предложение пояснения и по одной дословной цитате доклада и
положения для высокой уверенности. Решение по порогам принимает код: от RELEVANT_FROM —
интерес есть, до NOT_RELEVANT_UPTO — интереса нет, между ними — решение оператора.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rapporteur import db, departments, model_choice
from rapporteur.contracts import ModelProfile, TalkFragmentPack
from rapporteur.contracts.enums import TextOrigin
from rapporteur.errors import NotFoundError, StageError, UserError
from rapporteur.llm.client import LlmClient
from rapporteur.llm.tasks import json_task
from rapporteur.storage import Storage, cache_key
from rapporteur.summarize.quotes import QuoteConfig, match_quote

log = logging.getLogger("rapporteur.screening")
VERSION = "2.0.0"
# Пороги уверенности модели, %: от RELEVANT_FROM — интерес есть, до NOT_RELEVANT_UPTO
# включительно — интереса нет, между ними решает оператор.
RELEVANT_FROM = 70
NOT_RELEVANT_UPTO = 30
# Пояснение просится до _REASON_ASKED символов; схема допускает запас до _REASON_LENGTH,
# потому что Ollama обрывает ответ ровно на пределе схемы, в том числе посреди слова.
_REASON_ASKED = 120
_REASON_LENGTH = 200


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=10, max_length=240)


class InterestAnswer(BaseModel):
    """Ответ модели по одному отделу: короткий, без развёрнутого обоснования."""

    model_config = ConfigDict(extra="forbid")
    interest: int = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=_REASON_LENGTH)
    evidence: list[Quote] = Field(max_length=1)
    paragraphs: list[Quote] = Field(max_length=1)

    @model_validator(mode="after")
    def supported_interest(self) -> "InterestAnswer":
        if self.interest >= RELEVANT_FROM and (not self.evidence or not self.paragraphs):
            raise ValueError(
                f"Для уверенности от {RELEVANT_FROM} % нужны цитаты доклада и положения"
            )
        return self


def status_of(interest: int) -> Literal["relevant", "not_relevant", "uncertain"]:
    if interest >= RELEVANT_FROM:
        return "relevant"
    if interest <= NOT_RELEVANT_UPTO:
        return "not_relevant"
    return "uncertain"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["relevant", "not_relevant", "uncertain"]
    # Уверенность модели, что доклад интересен отделу, %; None — решение без модели.
    interest: int | None = Field(default=None, ge=0, le=100)
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
    stale = snapshot[:2] != (row["input_revision"], row["department_revisions"]) or (
        not row["manual"] and snapshot[2] != row["profile"]
    )
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
        # Допуск к полной обработке: интерес, подтверждённый оператором (interest.py).
        "eligible": state == "relevant" and bool(row["manual"]),
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
    # Решение оператора заменяет только статусы: проценты и описания модели
    # сохраняются, чтобы отбор можно было пересмотреть без пересчёта.
    previous = {item["department_id"]: item for item in current["items"]}
    items = []
    for dep in deps:
        old = previous.get(dep["id"], {})
        item = {
            "department_id": dep["id"],
            "department_version": dep["version"],
            "department_name": dep["name"],
            "status": "relevant" if dep["id"] in selected else "not_relevant",
            "reason": old.get("reason")
            or (
                "Интерес подтверждён оператором" if dep["id"] in selected else "Исключён оператором"
            ),
            "evidence": old.get("evidence", []),
            "paragraphs": old.get("paragraphs", []),
        }
        if old.get("interest") is not None:
            item["interest"] = old["interest"]
        items.append(item)
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
    schema = InterestAnswer.model_json_schema()
    for name, texts in (("evidence", source_text), ("paragraphs", regulation_text)):
        item = Quote.model_json_schema()
        item["properties"]["label"]["enum"] = list(texts)
        schema["properties"][name]["items"] = item

    def check(value: InterestAnswer) -> list[str]:
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

    answer = json_task(
        client,
        profile,
        "Оцени, интересен ли доклад отделу по его обязанностям из положения. "
        "interest — уверенность в процентах от 0 до 100, что доклад интересен отделу: "
        f"от {RELEVANT_FROM} — тема доклада прямо относится к конкретной обязанности отдела, "
        f"до {NOT_RELEVANT_UPTO} — связи нет. Совпадения общих слов недостаточно. "
        f"reason — одно короткое предложение до {_REASON_ASKED} символов: какая "
        "обязанность отдела связана с докладом или почему связи нет, без пересказа доклада. "
        f"При interest от {RELEVANT_FROM} приведи одну дословную цитату доклада в evidence "
        "и одну дословную цитату положения в paragraphs, иначе оставь списки пустыми. "
        "Это предварительный отбор, не итоговая рекомендация. Ответ по-русски.",
        db.dumps({"sources": sources, "regulations": regulations}),
        InterestAnswer,
        task=task,
        schema=schema,
        check=check,
    )
    status = status_of(answer.interest)
    reason = _whole_words(answer.reason)
    if status == "relevant" and all(item.label.startswith("V") for item in answer.evidence):
        status, reason = "uncertain", "Связь видна только в модельном описании изображения."
    return Decision(
        status=status,
        interest=answer.interest,
        reason=reason,
        evidence=answer.evidence,
        paragraphs=answer.paragraphs,
    )


def _whole_words(reason: str) -> str:
    """Пояснение, оборванное на пределе схемы, укорачивается до целого слова."""
    if len(reason) < _REASON_LENGTH:
        return reason
    cut = reason[: reason.rfind(" ")].rstrip(" ,;:—-") if " " in reason else reason
    return cut + "…"


def run_once(
    database: db.Database,
    storage: Storage,
    factory: Callable[[ModelProfile], LlmClient],
    pack_factory: Callable[[str], TalkFragmentPack],
) -> str | None:
    with database.tx() as conn:
        row = db.one(
            conn,
            "SELECT s.* FROM talk_screenings s JOIN talks t ON t.id=s.talk_id "
            "WHERE s.job='queued' AND t.worker_active=0 AND s.version=(SELECT "
            "max(version) FROM talk_screenings x WHERE x.talk_id=s.talk_id) "
            "ORDER BY created_at LIMIT 1",
        )
        if row is None:
            return None
        talk_id: str = row["talk_id"]
        version = row["version"]
        if _snapshot(conn, talk_id) != (
            row["input_revision"],
            row["department_revisions"],
            row["profile"],
        ) and departments.latest(conn):
            conn.execute(
                "UPDATE talk_screenings SET job='done' WHERE talk_id=? AND version=?",
                (talk_id, version),
            )
            queue(conn, talk_id, force=True)
            return talk_id
        conn.execute("UPDATE talks SET worker_active=1 WHERE id=?", (talk_id,))
        conn.execute(
            "UPDATE talk_screenings SET job='running' WHERE talk_id=? AND version=?",
            (talk_id, version),
        )
        log.info("Отбор по интересу отделов: доклад %s, версия %s", talk_id, version)
    started = time.monotonic()
    try:
        profile = ModelProfile.model_validate_json(row["profile"])
        with database.read() as conn:
            deps = departments.require_regulations(conn)
            previous = db.one(
                conn,
                "SELECT department_revisions,result FROM talk_screenings WHERE talk_id=? "
                "AND input_revision=? AND manual=1 AND version<? ORDER BY version DESC LIMIT 1",
                talk_id,
                row["input_revision"],
                version,
            )
        preserved = (
            {item["department_id"]: item for item in json.loads(previous["result"])}
            if previous and previous["department_revisions"] != row["department_revisions"]
            else {}
        )
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
        readable = any(
            f.text.strip() and f.origin is not TextOrigin.VLM and not f.low_quality
            for f in pack.fragments
        )
        evidence_links = {
            fragment.label: f"/api/v1/files/{fragment.source_file_id}/content"
            for fragment in pack.fragments
        }
        results = []
        for dep in deps:
            confirmed = preserved.get(dep["id"])
            if confirmed and confirmed["department_version"] == dep["version"]:
                results.append(confirmed)
                continue
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
            # Самое сильное решение по отделу: интерес, затем неопределённость, затем отказ;
            # внутри одного решения — наибольшая уверенность.
            chosen = (
                min(decisions, key=lambda d: (priority[d.status], -(d.interest or 0)))
                if decisions
                else Decision(
                    status="uncertain",
                    reason="Недостаточно извлечённых сведений",
                    evidence=[],
                    paragraphs=[],
                )
            )
            if chosen.status == "not_relevant" and not readable:
                chosen = Decision(
                    status="uncertain",
                    reason="Недостаточно читаемого текста для уверенного исключения доклада",
                    evidence=[],
                    paragraphs=[],
                )
            results.append(
                {
                    "department_id": dep["id"],
                    "department_version": dep["version"],
                    "department_name": dep["name"],
                    **chosen.model_dump(),
                    "evidence": [
                        {**quote.model_dump(), "source_url": evidence_links.get(quote.label)}
                        for quote in chosen.evidence
                    ],
                    "paragraphs": [
                        {
                            **quote.model_dump(),
                            "source_url": (
                                f"/api/v1/departments/{dep['id']}/source?version={dep['version']}"
                                if dep["sha256"] and not quote.label.startswith("p0.")
                                else None
                            ),
                        }
                        for quote in chosen.paragraphs
                    ],
                }
            )
        with database.tx() as conn:
            current = _latest(conn, talk_id)
            if current is None or current["version"] != version:
                conn.execute(
                    "UPDATE talk_screenings SET job='done',result=? WHERE talk_id=? AND version=?",
                    (db.dumps(results), talk_id, version),
                )
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
    finally:
        with database.tx() as conn:
            conn.execute("UPDATE talks SET worker_active=0 WHERE id=?", (talk_id,))
        log.info("stage=screening seconds=%.3f", time.monotonic() - started)
    return talk_id
