"""Versioned department regulations and evidence-linked recommendations."""

import json
import logging
import sqlite3
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from rapporteur import db, model_choice
from rapporteur.contracts import ModelProfile, Summary
from rapporteur.contracts.enums import ClaimStatus
from rapporteur.errors import NotFoundError, UserError
from rapporteur.llm.client import LlmClient
from rapporteur.llm.tasks import json_task

log = logging.getLogger("rapporteur.departments")


class Recommendation(BaseModel):
    department_id: str
    department_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=3000)
    claim_ids: list[str] = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1)


class Matches(BaseModel):
    items: list[Recommendation]


class Review(BaseModel):
    version: int = Field(ge=1)
    items: list[Recommendation]


def latest(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {**dict(row), "pages": json.loads(row["pages"])}
        for row in conn.execute(
            "SELECT * FROM departments d WHERE version=(SELECT max(version) "
            "FROM departments x WHERE x.id=d.id) "
            "ORDER BY name"
        )
    ]


def revisions(conn: sqlite3.Connection) -> dict[str, int]:
    return {item["id"]: item["version"] for item in latest(conn)}


def remove(conn: sqlite3.Connection, department_id: str) -> None:
    """Отдел удаляется со всеми версиями положения. Хранимые ссылки на него в отборе и
    рекомендациях устаревают сами: view() сравнивает сохранённые версии отделов с
    текущими, а отсутствующий отдел больше не входит в этот словарь."""
    if db.one(conn, "SELECT 1 FROM departments WHERE id=?", department_id) is None:
        raise NotFoundError("Отдел не найден")
    conn.execute("DELETE FROM departments WHERE id=?", (department_id,))


def require_regulations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    usable = [dep for dep in latest(conn) if any(p["text"].strip() for p in dep["pages"])]
    if not usable:
        raise UserError(
            "Сначала загрузите положение хотя бы одного отдела в разделе «Отделы».",
            409,
            code="departments_required",
        )
    return usable


def paragraphs(department: dict[str, Any]) -> dict[str, str]:
    result = {}
    for page in department["pages"]:
        # Stable page + chunk locators, including when OCR has no paragraph breaks.
        text = page["text"]
        for index, start in enumerate(range(0, len(text), 2000), 1):
            result[f"p{page['page']}.{index}"] = text[start : start + 2000]
    return result


def view(conn: sqlite3.Connection, talk_id: str) -> dict[str, Any]:
    row = db.one(conn, "SELECT * FROM recommendations WHERE talk_id=?", talk_id)
    if not row:
        return {"version": 0, "items": [], "reviewed": False, "stale": False}
    current = db.one(conn, "SELECT max(number) AS number FROM summaries WHERE talk_id=?", talk_id)
    result = dict(row)
    result["items"] = json.loads(row["items"])
    result["stale"] = (
        current is None or row["summary_version"] != current["number"]
    ) or json.loads(row["department_revisions"]) != revisions(conn)
    return result


def validate(
    items: list[Recommendation], summary: Summary, departments: list[dict[str, Any]]
) -> None:
    claims = {c.key for c in summary.claims if c.status is ClaimStatus.CONFIRMED}
    known = {d["id"]: d for d in departments}
    for item in items:
        dep = known.get(item.department_id)
        if (
            dep is None
            or dep["version"] != item.department_version
            or not set(item.claim_ids) <= claims
            or not set(item.paragraph_ids) <= paragraphs(dep).keys()
        ):
            raise UserError("Рекомендация ссылается на неизвестный факт или пункт положения")


def review(conn: sqlite3.Connection, talk_id: str, data: Review) -> None:
    row = view(conn, talk_id)
    if row["version"] != data.version or row["stale"]:
        raise UserError("Рекомендации изменились или устарели: выполните расчет заново", 409)
    summary_row = db.one(
        conn, "SELECT summary FROM summaries WHERE talk_id=? ORDER BY number DESC LIMIT 1", talk_id
    )
    if summary_row is None:
        raise UserError("Нет сводки")
    summary = Summary.model_validate_json(summary_row["summary"])
    validate(data.items, summary, latest(conn))
    conn.execute(
        "UPDATE recommendations SET items=?, reviewed=1, version=version+1 WHERE talk_id=?",
        (db.dumps([i.model_dump() for i in data.items]), talk_id),
    )


def queue(conn: sqlite3.Connection, talk_id: str) -> None:
    talk = db.one(conn, "SELECT status FROM talks WHERE id=?", talk_id)
    if talk is None or talk["status"] != "reviewed":
        raise UserError("Сначала подтвердите сводку доклада")
    if not latest(conn):
        raise UserError("Сначала загрузите положения отделов")
    conn.execute(
        "INSERT INTO recommendation_jobs(talk_id, job) VALUES (?, 'queued') "
        "ON CONFLICT(talk_id) DO UPDATE SET job='queued', error=NULL",
        (talk_id,),
    )


def run_once(database: db.Database, factory: Callable[[ModelProfile], LlmClient]) -> bool:
    with database.tx() as conn:
        job = db.one(conn, "SELECT * FROM recommendation_jobs WHERE job='queued' LIMIT 1")
        if job is None:
            return False
        talk_id = job["talk_id"]
        conn.execute("UPDATE recommendation_jobs SET job='running' WHERE talk_id=?", (talk_id,))
        log.info("Рекомендации отделам: доклад %s", talk_id)
        summary_row = db.one(
            conn, "SELECT * FROM summaries WHERE talk_id=? ORDER BY number DESC LIMIT 1", talk_id
        )
        deps = latest(conn)
        revs = revisions(conn)
        profile = model_choice.active_profile(conn)
    try:
        if summary_row is None:
            raise UserError("Нет сводки")
        summary = Summary.model_validate_json(summary_row["summary"])
        claims = [c for c in summary.claims if c.status is ClaimStatus.CONFIRMED]
        result = []
        client = factory(profile)
        for dep in deps:
            for paragraph_id, text in paragraphs(dep).items():
                for offset in range(0, len(claims), 12):
                    batch = claims[offset : offset + 12]
                    content = json.dumps(
                        {
                            "department_id": dep["id"],
                            "department_version": dep["version"],
                            "name": dep["name"],
                            "paragraph_id": paragraph_id,
                            "regulation": text,
                            "facts": [{"id": c.key, "text": c.text} for c in batch],
                        },
                        ensure_ascii=False,
                    )
                    schema = Matches.model_json_schema()
                    schema["properties"]["items"]["maxItems"] = 3
                    fields = schema["$defs"]["Recommendation"]["properties"]
                    fields["department_id"]["enum"] = [dep["id"]]
                    fields["department_version"]["enum"] = [dep["version"]]
                    fields["reason"]["maxLength"] = 600
                    fields["claim_ids"]["items"]["enum"] = [c.key for c in batch]
                    fields["claim_ids"]["maxItems"] = len(batch)
                    fields["paragraph_ids"]["items"]["enum"] = [paragraph_id]
                    fields["paragraph_ids"]["maxItems"] = 1
                    found = json_task(
                        client,
                        profile,
                        "Оцени, интересны ли факты доклада отделу по его обязанностям. "
                        "При отсутствии конкретной связи верни пустой items. Не назначай "
                        "отдел принудительно. "
                        "Обоснование по-русски, с точными claim_ids и paragraph_ids из входа.",
                        content,
                        Matches,
                        schema=schema,
                        task=f"Рекомендации {talk_id}, отдел {dep['name']}, {paragraph_id}",
                    )
                    validate(found.items, summary, [dep])
                    allowed = {c.key for c in batch}
                    if any(
                        not set(i.claim_ids) <= allowed or set(i.paragraph_ids) != {paragraph_id}
                        for i in found.items
                    ):
                        raise UserError("Модель сослалась на материал вне текущей порции")
                    result.extend(found.items)
        with database.tx() as conn:
            current = db.one(
                conn, "SELECT max(number) AS n FROM summaries WHERE talk_id=?", talk_id
            )
            talk = db.one(conn, "SELECT status FROM talks WHERE id=?", talk_id)
            if (
                current is None
                or talk is None
                or current["n"] != summary.version
                or revisions(conn) != revs
                or talk["status"] != "reviewed"
            ):
                raise UserError("Источники изменились во время расчета: запустите заново", 409)
            conn.execute(
                "INSERT INTO recommendations(talk_id, summary_version, department_revisions, "
                "version, items, reviewed) VALUES (?, ?, ?, 1, ?, 0) ON "
                "CONFLICT(talk_id) DO UPDATE SET "
                "summary_version=excluded.summary_version, "
                "department_revisions=excluded.department_revisions, "
                "version=recommendations.version+1, items=excluded.items, reviewed=0",
                (
                    talk_id,
                    summary.version,
                    db.dumps(revs),
                    db.dumps([i.model_dump() for i in result]),
                ),
            )
            conn.execute(
                "UPDATE recommendation_jobs SET job='done', error=NULL WHERE talk_id=?", (talk_id,)
            )
    except Exception as error:
        with database.tx() as conn:
            conn.execute(
                "UPDATE recommendation_jobs SET job='failed', error=? WHERE talk_id=?",
                (str(error)[:1000], talk_id),
            )
    return True


def report_snapshot(
    conn: sqlite3.Connection, conference_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    deps = {d["id"]: d for d in latest(conn)}
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    if not deps:
        return result, warnings
    for talk in db.conference_talks(conn, conference_id):
        # Рекомендации строятся по фактам сводки; у доклада только с карточкой их нет
        # (interest.py), прежние рекомендации его сводки в справку не идут.
        if talk["status"] not in ("reviewed", "processed", "stale") or talk["card_only"]:
            continue
        recs = view(conn, talk["id"])
        if not recs["version"] or recs["stale"] or not recs["reviewed"]:
            warnings.append(f"Рекомендации отделам не проверены или устарели: {talk['title']}")
            continue
        summary = db.one(
            conn,
            "SELECT summary FROM summaries WHERE talk_id=? ORDER BY number DESC LIMIT 1",
            talk["id"],
        )
        assert summary is not None
        claims = {c.key: c.text for c in Summary.model_validate_json(summary["summary"]).claims}
        for item in recs["items"]:
            dep = deps[item["department_id"]]
            result.append(
                {
                    **item,
                    "talk_id": talk["id"],
                    "department_name": dep["name"],
                    "regulation_name": dep["original_name"],
                    "regulation_sha256": dep["sha256"],
                    "paragraphs": {key: paragraphs(dep)[key] for key in item["paragraph_ids"]},
                    "facts": {key: claims[key] for key in item["claim_ids"]},
                }
            )
    return result, warnings
