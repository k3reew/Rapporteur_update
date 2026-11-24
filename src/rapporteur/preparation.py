"""Cached multimodal extraction and reviewable conference segmentation."""

import base64
import json
import logging
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rapporteur import db, materials, model_choice
from rapporteur.contracts import FragmentDraft, ModelProfile, Speaker
from rapporteur.contracts.base import NonEmptyStr
from rapporteur.contracts.enums import FileKind, FragmentKind, TextOrigin
from rapporteur.contracts.fragments import PageLocator, TimeLocator
from rapporteur.errors import StageError, UserError
from rapporteur.ingest import card, documents, notes, titles
from rapporteur.llm.client import LlmClient
from rapporteur.llm.tasks import json_task
from rapporteur.storage import Storage, blob_key, cache_key

log = logging.getLogger("rapporteur.preparation")

VERSION = "2.0.0"
_RECORDINGS = (FileKind.AUDIO, FileKind.VIDEO)
_UNTITLED = "Тема не установлена"
ASR_DISABLED = "Распознавание речи выключено: архивная запись без расшифровки"
AuthorName = Annotated[str, Field(min_length=1, max_length=200)]


def candidate_windows(
    candidates: list[dict[str, Any]], budget: int
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for candidate in candidates:
        if batch and len(json.dumps([*batch, candidate], ensure_ascii=False)) > budget:
            yield batch
            batch = []
        batch.append(candidate)
    yield batch


class Prepared(BaseModel):
    fragments: list[FragmentDraft]
    images: dict[int, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Visual(BaseModel):
    description: str = Field(max_length=1500)


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: str
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=1)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def interval(self) -> Self:
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("Укажите начало и конец интервала")
        if self.start_ms is not None and self.end_ms is not None and self.start_ms >= self.end_ms:
            raise ValueError("Начало должно быть раньше конца")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("Укажите обе границы страниц")
        if (
            self.page_start is not None
            and self.page_end is not None
            and self.page_start > self.page_end
        ):
            raise ValueError("Начальная страница больше конечной")
        if self.start_ms is not None and self.page_start is not None:
            raise ValueError("Нельзя одновременно указать страницы и время")
        return self


class ProposedTalk(BaseModel):
    talk_id: str | None = None
    title: str = Field(min_length=1, max_length=1000)
    speakers: list[Speaker] = Field(default_factory=list)
    # Карточка: организации и контакты, не отнесённые к автору.
    organizations: list[NonEmptyStr] = Field(default_factory=list)
    contacts: list[NonEmptyStr] = Field(default_factory=list)
    links: list[Link] = Field(min_length=1)
    reason: str = ""
    section: NonEmptyStr | None = None


class ProposalWrite(BaseModel):
    version: int = Field(ge=1)
    talks: list[ProposedTalk] = Field(max_length=500)
    context_file_ids: list[str] | None = None


class PreparationStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_start: bool = False
    force: bool = False
    workflow: Literal["legacy", "operator"] = "legacy"


class Boundary(BaseModel):
    index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=1000)
    speakers: list[AuthorName] = Field(default_factory=list, max_length=20)


class Boundaries(BaseModel):
    talks: list[Boundary]


class MaterialMatch(BaseModel):
    title: Annotated[str, Field(max_length=1000)] | None
    speakers: list[AuthorName] = Field(max_length=20)
    talk_index: int | None = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=600)
    role: Literal["talk", "context"] = "talk"
    section: Annotated[str, Field(max_length=200)] | None = None


def transcript_windows(texts: list[str], budget: int) -> Iterator[list[tuple[int, str]]]:
    """Budgeted overlapping windows retain absolute ASR indices, including split segments."""
    width = max(1, (budget - 40) // 2)
    parts = [
        (i, text[start : start + width])
        for i, text in enumerate(texts)
        for start in range(0, len(text), width)
    ]
    start = 0
    while start < len(parts):
        end, size = start, 0
        while end < len(parts) and end - start < 100:
            added = len(parts[end][1]) + 20
            if size + added > budget:
                break
            size += added
            end += 1
        yield parts[start:end]
        if end == len(parts):
            return
        start = max(start + 1, end - 5)


def prepared_key(file: db.Row, profile_revision: str, *, asr_revision: str = "") -> str:
    profile_revision += asr_revision
    if file["kind"] in (FileKind.DOCUMENT, FileKind.PROGRAM, FileKind.PRESENTATION):
        profile_revision += f":document-{documents.VERSION}"
    return (
        f"cache/prepared/{cache_key(VERSION, file['sha256'], file['kind'], profile_revision)}.json"
    )


def extract(
    file: db.Row,
    storage: Storage,
    client: LlmClient,
    profile: ModelProfile,
) -> Prepared:
    key = prepared_key(file, profile.model_dump_json())
    if file["kind"] in _RECORDINGS:
        if storage.exists(key):
            return Prepared.model_validate_json(storage.get(key))
        # Historical ASR revisions used model-specific cache keys. Match by source hash.
        for path in storage.path("cache/prepared").glob("*.json"):
            try:
                cached = Prepared.model_validate_json(path.read_bytes())
            except (OSError, ValueError):
                continue
            if cached.fragments and all(
                fragment.kind == FragmentKind.TRANSCRIPT
                and fragment.source_sha256 == file["sha256"]
                for fragment in cached.fragments
            ):
                return cached
        return Prepared(fragments=[], warnings=[ASR_DISABLED])

    def compute() -> Prepared:
        sha, kind = file["sha256"], file["kind"]
        data = storage.get(blob_key(sha))
        if kind == FileKind.NOTES:
            return Prepared(fragments=notes.parse_notes(data, sha).fragments)
        images, fragments = {}, []
        warnings = []
        if kind == FileKind.PRESENTATION:
            # Keep the original parser and cache layout for existing slide links.
            import tempfile
            from pathlib import Path

            from rapporteur.contracts import SlidesArtifact
            from rapporteur.ingest.slides import parse_slides
            from rapporteur.processing import slides_cache_key

            with tempfile.TemporaryDirectory(prefix="slides-") as tmp:
                artifact = storage.cached(
                    f"cache/slides/{slides_cache_key(sha)}.json",
                    SlidesArtifact,
                    lambda: parse_slides(
                        data, sha, storage, f"slides/{slides_cache_key(sha)}", Path(tmp)
                    ),
                )
            fragments.extend(artifact.fragments)
            warnings.extend(artifact.warnings)
            for slide in artifact.slides:
                images[slide.slide_no] = slide.image_path
                if not slide.has_text:
                    import tempfile
                    from pathlib import Path

                    with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
                        text = documents.ocr(storage.get(slide.image_path), Path(tmp))
                    if text:
                        fragments.append(
                            documents.fragment(
                                sha, slide.slide_no, text, TextOrigin.OCR, FragmentKind.SLIDE
                            )
                        )
        else:
            for number, (text, image, origin) in enumerate(
                documents.pages(data, file["media_type"]), 1
            ):
                if text.strip():
                    fragments.append(documents.fragment(sha, number, text, origin))
                if image:
                    image_key = f"pages/{sha}/page-{number:03}.png"
                    storage.put(image_key, image)
                    images[number] = image_key
        # Text documents do not need visual interpretation; all presentation/image pages do.
        if kind in (FileKind.PRESENTATION, FileKind.IMAGE):
            started = time.monotonic()
            for number, image_key in images.items():
                digest = cache_key(VERSION, sha, number, profile.model_dump_json())
                visual_key = f"cache/vision/{digest}.json"

                def describe(number: int = number, image_key: str = image_key) -> Visual:
                    return json_task(
                        client,
                        profile,
                        "Опиши содержательные графики, схемы и изображения на слайде по-русски. "
                        "Не пересказывай обычный текст. Не угадывай мелкие числа. "
                        "Если графиков или схем нет, description должен быть пустым.",
                        "Слайд " + str(number),
                        Visual,
                        (base64.b64encode(storage.get(image_key)).decode(),),
                        task=f"Визуальный разбор {file['relative_path']}, страница {number}",
                    )

                visual = storage.cached(visual_key, Visual, describe)
                if visual.description.strip():
                    fragments.append(
                        documents.fragment(
                            sha,
                            number,
                            visual.description,
                            TextOrigin.VLM,
                            FragmentKind.SLIDE_DESCRIPTION,
                        )
                    )
            log.info(
                "stage=vision kind=%s pages=%d seconds=%.3f",
                kind,
                len(images),
                time.monotonic() - started,
            )
        return Prepared(fragments=fragments, images=images, warnings=warnings)

    return storage.cached(key, Prepared, compute)


def queue(
    conn: sqlite3.Connection,
    conference_id: str,
    *,
    auto_start: bool = False,
    force: bool = False,
    workflow: str = "legacy",
) -> None:
    from rapporteur.departments import require_regulations

    require_regulations(conn)
    materials.require_conference(conn, conference_id)
    row = db.one(
        conn,
        "SELECT job,auto_start,input_snapshot,workflow FROM preparations WHERE conference_id=?",
        conference_id,
    )
    if row and row["workflow"] == "operator":
        workflow = "operator"
    auto_start = auto_start or workflow == "operator"
    if row and row["job"] in ("queued", "running"):
        if auto_start and row["auto_start"] and row["workflow"] == workflow:
            return
        raise UserError("Подготовка уже выполняется", 409)
    if auto_start and not db.one(
        conn,
        "SELECT 1 FROM files WHERE conference_id=? AND kind NOT IN ('program','audio','video')",
        conference_id,
    ):
        raise UserError("Загрузите материал хотя бы одного доклада")
    if (
        auto_start
        and not force
        and row
        and row["auto_start"]
        and row["workflow"] == workflow
        and row["job"] in ("done", "confirmed")
        and row["input_snapshot"] == input_snapshot(conn, conference_id)
    ):
        return
    conn.execute(
        "INSERT INTO preparations(conference_id,job,auto_start,workflow) VALUES (?,'queued',?,?) "
        "ON CONFLICT(conference_id) DO UPDATE SET job='queued',error=NULL,version=version+1, "
        "auto_start=max(auto_start,excluded.auto_start),workflow=excluded.workflow,"
        "input_snapshot=NULL,screening_confirmed=0,review_reasons='[]'",
        (conference_id, auto_start, workflow),
    )


def materials_changed(conn: sqlite3.Connection, conference_id: str) -> None:
    conn.execute(
        "UPDATE preparations SET version=version+1, error=NULL, review_reasons='[]', "
        "screening_confirmed=0, "
        "job=CASE WHEN auto_start THEN 'awaiting_start' ELSE 'queued' END WHERE conference_id=?",
        (conference_id,),
    )


def automatic(conn: sqlite3.Connection, conference_id: str) -> bool:
    row = db.one(conn, "SELECT auto_start FROM preparations WHERE conference_id=?", conference_id)
    return bool(row and row["auto_start"])


def operator(conn: sqlite3.Connection, conference_id: str) -> bool:
    row = db.one(conn, "SELECT workflow FROM preparations WHERE conference_id=?", conference_id)
    return bool(row and row["workflow"] == "operator")


def screening_talks(conn: sqlite3.Connection, conference_id: str) -> list[db.Row]:
    row = db.one(
        conn,
        "SELECT proposal FROM preparations WHERE conference_id=? AND job='confirmed'",
        conference_id,
    )
    ids = {item["talk_id"] for item in json.loads(row["proposal"])} if row else set()
    return [talk for talk in db.conference_talks(conn, conference_id) if talk["id"] in ids]


def screening_confirmed(conn: sqlite3.Connection, conference_id: str) -> bool:
    from rapporteur import screening

    row = db.one(
        conn,
        "SELECT 1 FROM preparations WHERE conference_id=? AND job='confirmed' "
        "AND screening_confirmed=1",
        conference_id,
    )
    if not row:
        return False
    talks = screening_talks(conn, conference_id)
    return bool(talks) and all(
        result["manual"] and not result["stale"]
        for talk in talks
        for result in [screening.view(conn, talk["id"])]
    )


def input_snapshot(conn: sqlite3.Connection, conference_id: str) -> str:
    files = conn.execute(
        "SELECT id,sha256,kind,talk_id,confirmed FROM files WHERE conference_id=? ORDER BY id",
        (conference_id,),
    )
    talks = db.conference_talks(conn, conference_id)
    return cache_key(
        [tuple(file) for file in files],
        [
            (
                talk["id"],
                talk["input_revision"],
                talk["title"],
                talk["speakers"],
                talk["organizations"],
                talk["contacts"],
            )
            for talk in talks
        ],
    )


def review_reasons(
    files: list[db.Row],
    extracted: dict[str, Prepared],
    proposal: list[ProposedTalk],
    *,
    existing_registry: bool,
    context_ids: set[str] | None = None,
) -> list[str]:
    reasons = []
    if existing_registry:
        reasons.append("Сверьте изменения с ранее подтверждённым реестром докладов.")
    if not proposal:
        reasons.append("Доклады не найдены. Проверьте материалы и их назначение.")
    linked = {link.file_id for talk in proposal for link in talk.links}
    for file in files:
        name = file["relative_path"]
        if file["kind"] == FileKind.PROGRAM or file["id"] in (context_ids or ()):
            continue
        result = extracted[file["id"]]
        if not result.fragments or file["id"] not in linked:
            detail = " ".join(result.warnings) or "Не удалось извлечь содержание или связать файл."
            reasons.append(f"{name}: {detail}")
        elif file["kind"] in (FileKind.AUDIO, FileKind.VIDEO):
            reasons.append(f"{name}: проверьте границы докладов в записи.")
        elif result.warnings:
            reasons.append(f"{name}: {' '.join(result.warnings)}")
    for talk in proposal:
        if "(тема предварительная)" in talk.title or talk.reason.startswith("Нужна проверка:"):
            reasons.append(f"{talk.title}: {talk.reason}")
    return reasons


def state(conn: sqlite3.Connection, conference_id: str) -> dict[str, object]:
    materials.require_conference(conn, conference_id)
    row = db.one(conn, "SELECT * FROM preparations WHERE conference_id = ?", conference_id)
    warnings = _warnings(conn, conference_id)
    if not row:
        return {
            "version": 0,
            "job": None,
            "error": None,
            "talks": [],
            "warnings": warnings,
            "auto_start": False,
            "review_reasons": [],
            "workflow": "legacy",
            "context_file_ids": [],
        }
    proposal = json.loads(row["proposal"])
    if row["job"] == "confirmed":
        talks = {talk["id"]: talk for talk in db.conference_talks(conn, conference_id)}
        for item in proposal:
            if (talk := talks.get(item.get("talk_id"))) is not None:
                item.update(_card_fields(talk))
                title = talk["section_title"]
                item["section"] = (
                    None
                    if row["workflow"] == "operator" and title == materials.DEFAULT_SECTION
                    else title
                )
    return {
        "version": row["version"],
        "job": row["job"],
        "error": row["error"],
        "talks": proposal,
        "warnings": warnings,
        "auto_start": bool(row["auto_start"]),
        "review_reasons": json.loads(row["review_reasons"]),
        "workflow": row["workflow"],
        "context_file_ids": json.loads(row["context_file_ids"]),
    }


def _card_fields(talk: db.Row) -> dict[str, Any]:
    return {
        "title": talk["title"] or _UNTITLED,
        "speakers": json.loads(talk["speakers"]),
        "organizations": json.loads(talk["organizations"]),
        "contacts": json.loads(talk["contacts"]),
    }


def sync_talk_card(conn: sqlite3.Connection, talk_id: str) -> None:
    row = db.one(
        conn,
        "SELECT preparations.* FROM talks JOIN sections ON sections.id=talks.section_id "
        "JOIN preparations ON preparations.conference_id=sections.conference_id WHERE talks.id=?",
        talk_id,
    )
    if row is None:
        return
    proposal = json.loads(row["proposal"])
    matching = [item for item in proposal if item.get("talk_id") == talk_id]
    if not matching and row["job"] != "running":
        return
    talk = db.one(conn, "SELECT * FROM talks WHERE id=?", talk_id)
    assert talk is not None
    for item in matching:
        item.update(_card_fields(talk))
    # A running worker captured the old card. Its version check discards that result.
    conn.execute(
        "UPDATE preparations SET proposal=?, version=version+1, "
        "job=CASE WHEN job='running' THEN 'queued' ELSE job END, "
        "input_snapshot=CASE WHEN job='confirmed' AND workflow='operator' THEN ? "
        "ELSE input_snapshot END WHERE conference_id=?",
        (db.dumps(proposal), input_snapshot(conn, row["conference_id"]), row["conference_id"]),
    )


def _warnings(conn: sqlite3.Connection, conference_id: str) -> list[str]:
    recordings = conn.execute(
        "SELECT count(*) FROM files WHERE conference_id = ? AND kind IN (?, ?)",
        (conference_id, *_RECORDINGS),
    ).fetchone()[0]
    if not recordings:
        return []
    return [
        f"Распознавание речи выключено, архивных записей: {recordings}. "
        "Доклады по нерасшифрованным записям не предлагаются, их текст не входит в сводки. "
        "Ранее сохранённые расшифровки доступны."
    ]


def slice_fragments(fragments: list[FragmentDraft], link: Link) -> list[FragmentDraft]:
    result = []
    for fragment in fragments:
        loc = fragment.locator
        if link.start_ms is not None:
            if not isinstance(loc, TimeLocator):
                continue
            # Assign by start: no quote containing neighbouring speech is passed through.
            if loc.start_ms < link.start_ms or loc.end_ms > (link.end_ms or 0):
                continue
        if link.page_start is not None:
            if not isinstance(loc, PageLocator) or not link.page_start <= loc.page_no <= (
                link.page_end or 0
            ):
                continue
        result.append(fragment)
    return result


def apply(
    conn: sqlite3.Connection,
    conference_id: str,
    data: ProposalWrite,
    storage: Storage | None = None,
) -> bool:
    """Сохраняет реестр и возвращает, нужно ли снова запускать обработку."""
    materials.require_conference(conn, conference_id)
    current = db.one(conn, "SELECT * FROM preparations WHERE conference_id=?", conference_id)
    if (
        current is None
        or current["version"] != data.version
        or current["job"] not in ("done", "confirmed")
    ):
        raise UserError("Разбиение изменилось или ещё не готово: обновите страницу", 409)
    if current["auto_start"] and (current["input_snapshot"] != input_snapshot(conn, conference_id)):
        raise UserError("Материалы изменились. Запустите анализ заново.", 409)
    if current["auto_start"] and not data.talks:
        raise UserError("Подтвердите хотя бы один доклад")
    files = {
        r["id"]: r
        for r in conn.execute("SELECT * FROM files WHERE conference_id=?", (conference_id,))
    }
    talks = {r["id"]: r for r in db.conference_talks(conn, conference_id)}
    context_ids = set(
        data.context_file_ids
        if data.context_file_ids is not None
        else json.loads(current["context_file_ids"])
    )
    if context_ids - files.keys():
        raise UserError("Материалы контекста отсутствуют в конференции")
    if current["job"] == "confirmed" and context_ids == set(
        json.loads(current["context_file_ids"])
    ):
        previous_items = [
            ProposedTalk.model_validate(item) for item in json.loads(current["proposal"])
        ]
        by_id = {item.talk_id: item for item in previous_items}
        if (
            len(data.talks) == len(by_id) == len(previous_items)
            and {item.talk_id for item in data.talks} == by_id.keys()
            and all(item == by_id[item.talk_id] for item in data.talks)
        ):
            conn.executemany(
                "UPDATE talks SET position=? WHERE id=?",
                [(position, item.talk_id) for position, item in enumerate(data.talks, 1)],
            )
            conn.execute(
                "UPDATE preparations SET proposal=?,version=version+1,input_snapshot=? "
                "WHERE conference_id=?",
                (
                    db.dumps([item.model_dump(mode="json") for item in data.talks]),
                    input_snapshot(conn, conference_id),
                    conference_id,
                ),
            )
            return False
    sections = {
        row["title"].casefold(): row["id"]
        for row in conn.execute(
            "SELECT title,id FROM sections WHERE conference_id=?", (conference_id,)
        )
    }
    intervals: dict[str, list[tuple[int, int]]] = {}
    assigned = set()
    for item in data.talks:
        if item.talk_id and (item.talk_id not in talks or item.talk_id in assigned):
            raise UserError("Доклад отсутствует или повторяется")
        assigned.add(item.talk_id)
        for link in item.links:
            file = files.get(link.file_id)
            if (
                file is None
                or file["kind"] == FileKind.PROGRAM
                or link.file_id in context_ids
                or (current["workflow"] == "operator" and file["kind"] in _RECORDINGS)
            ):
                raise UserError("Недопустимый материал")
            recording = file["kind"] in (FileKind.AUDIO, FileKind.VIDEO)
            if recording != (link.start_ms is not None):
                raise UserError("Для записи укажите временной интервал, для документа — страницы")
            if link.page_start is not None and storage is not None:
                profile = model_choice.active_profile(conn)
                key = prepared_key(file, profile.model_dump_json())
                if not storage.exists(key):
                    raise UserError("Сначала подготовьте документ для выбора страниц")
                prepared = Prepared.model_validate_json(storage.get(key))
                numbers = {
                    f.locator.page_no
                    for f in prepared.fragments
                    if isinstance(f.locator, PageLocator)
                } | set(prepared.images)
                if not numbers or (link.page_end or 0) > max(numbers):
                    raise UserError("Диапазон страниц выходит за границы материала")
            if recording:
                if (link.end_ms or 0) > file["duration_ms"]:
                    raise UserError("Интервал выходит за длительность записи")
                intervals.setdefault(link.file_id, []).append(
                    (link.start_ms or 0, link.end_ms or 0)
                )
    for spans in intervals.values():
        spans.sort()
        if any(b[0] < a[1] for a, b in pairwise(spans)):
            raise UserError("Интервалы докладов одной записи пересекаются")
    new_workflow = current["workflow"] == "operator"
    file_filter = " AND kind NOT IN ('audio','video')" if new_workflow else ""
    previous = conn.execute(
        "SELECT DISTINCT material_links.talk_id FROM material_links JOIN files ON files.id=file_id "
        f"WHERE conference_id=?{file_filter} UNION SELECT talk_id FROM files WHERE "
        f"conference_id=? AND talk_id IS NOT NULL{file_filter}",
        (conference_id, conference_id),
    ).fetchall()
    original_links: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    if current["auto_start"]:
        for row in conn.execute(
            "SELECT l.talk_id,l.file_id,l.start_ms,l.end_ms,l.page_start,l.page_end "
            "FROM material_links l JOIN files f ON f.id=l.file_id "
            f"WHERE f.conference_id=?{file_filter}",
            (conference_id,),
        ):
            original_links[row["talk_id"]].add(tuple(row)[1:])
    conn.execute(
        "DELETE FROM material_links WHERE file_id IN "
        f"(SELECT id FROM files WHERE conference_id=?{file_filter})",
        (conference_id,),
    )
    changed = {r["talk_id"] for r in previous}
    conn.execute(
        f"UPDATE files SET confirmed=1, talk_id=NULL WHERE conference_id=?{file_filter}",
        (conference_id,),
    )
    saved = []
    for position, item in enumerate(data.talks, 1):
        section_id = None
        section = item.section or (materials.DEFAULT_SECTION if new_workflow else None)
        if section:
            name = section.casefold()
            if name not in sections:
                sections[name] = materials.add_section(conn, conference_id, section)
            section_id = sections[name]
        talk_id = item.talk_id or materials.add_talk(conn, conference_id, item.title, section_id)
        old = talks.get(talk_id)
        if old is not None and section_id is not None and section_id != old["section_id"]:
            conn.execute(
                "UPDATE talks SET section_id=?,import_folder=NULL WHERE id=?",
                (section_id, talk_id),
            )
        conn.execute("UPDATE talks SET position=? WHERE id=?", (position, talk_id))
        title: str | None = item.title
        if title == _UNTITLED and item.talk_id and talks[item.talk_id]["title"] is None:
            title = None
        fields = (
            title,
            db.dumps([s.model_dump(mode="json") for s in item.speakers]),
            db.dumps(item.organizations),
            db.dumps(item.contacts),
        )
        linked = {
            (link.file_id, link.start_ms, link.end_ms, link.page_start, link.page_end)
            for link in item.links
        }
        known = talks.get(talk_id)
        if (
            current["auto_start"]
            and known is not None
            and (section_id is None or section_id == known["section_id"])
            and linked == original_links[talk_id]
            and (fields[0], json.loads(fields[2]), json.loads(fields[3]))
            == (known["title"], json.loads(known["organizations"]), json.loads(known["contacts"]))
            and item.speakers
            == [Speaker.model_validate(speaker) for speaker in json.loads(known["speakers"])]
        ):
            changed.discard(talk_id)
        else:
            changed.add(talk_id)
            conn.execute(
                "UPDATE talks SET title=?, speakers=?, organizations=?, contacts=? WHERE id=?",
                (*fields, talk_id),
            )
        for link in item.links:
            conn.execute(
                "INSERT INTO material_links VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    db.new_id(),
                    link.file_id,
                    talk_id,
                    link.start_ms,
                    link.end_ms,
                    link.page_start,
                    link.page_end,
                ),
            )
            conn.execute("UPDATE files SET confirmed=1, talk_id=NULL WHERE id=?", (link.file_id,))
        saved.append(item.model_copy(update={"talk_id": talk_id}).model_dump(mode="json"))
    for talk_id in changed:
        if not conn.execute("SELECT 1 FROM material_links WHERE talk_id=?", (talk_id,)).fetchone():
            conn.execute(
                "UPDATE talks SET status='insufficient_data', input_revision=input_revision+1, "
                "job=NULL, step=NULL WHERE id=?",
                (talk_id,),
            )
            continue
        conn.execute(
            "UPDATE talks SET input_revision=input_revision+1, "
            "status=CASE WHEN status IN ('processed','reviewed','stale') THEN "
            "'stale' ELSE 'matched' END "
            "WHERE id=?",
            (talk_id,),
        )
    conn.execute(
        "UPDATE preparations SET proposal=?, version=version+1, "
        "job='confirmed',review_reasons='[]',input_snapshot=?,context_file_ids=?,"
        "screening_confirmed=0 WHERE conference_id=?",
        (
            db.dumps(saved),
            input_snapshot(conn, conference_id),
            db.dumps(sorted(context_ids)),
            conference_id,
        ),
    )
    return True


def propose(
    files: list[db.Row],
    extracted: dict[str, Prepared],
    existing: materials.Materials,
    client: LlmClient,
    profile: ModelProfile,
    storage: Storage | None = None,
    *,
    context_ids: set[str] | None = None,
    previous_proposal: list[ProposedTalk] | None = None,
) -> list[ProposedTalk]:
    proposed: list[ProposedTalk] = []
    known_talks = {talk.id: talk for talk in existing.talks}
    context_files = {file["id"] for file in files if file["kind"] == FileKind.PROGRAM}
    if context_ids is not None:
        context_files.update(context_ids)
        context_ids.update(context_files)
    by_id = {file["id"]: file for file in files}
    confirmed = {str(material.source.id): material for material in existing.files}
    for item in previous_proposal or ():
        if item.talk_id not in known_talks:
            continue
        links = [
            link
            for link in item.links
            if link.file_id in by_id
            and link.file_id not in context_files
            and item.talk_id in confirmed[link.file_id].range_talk_ids
        ]
        if links:
            proposed.append(item.model_copy(update={"links": links}))
    already_linked = {link.file_id for item in proposed for link in item.links}
    program = "\n".join(
        f.normalized_text
        for file in files
        if file["id"] in context_files
        for f in extracted[file["id"]].fragments
    )
    excerpts = {
        file["id"]: " ".join(f.normalized_text[:250] for f in extracted[file["id"]].fragments[:3])
        for file in files
    }
    for file in files:
        if file["kind"] not in (FileKind.AUDIO, FileKind.VIDEO):
            continue
        fragments = extracted[file["id"]].fragments
        time_starts = [f.locator.start_ms for f in fragments if isinstance(f.locator, TimeLocator)]
        boundaries = []
        # Overlap supplies context at a window boundary; deduplicate absolute starts.
        budget = max(
            1000,
            min(
                16000,
                (
                    profile.generation.context_tokens
                    - min(profile.generation.max_tokens, 6000)
                    - 4000
                )
                * 2,
            ),
        )
        for window in transcript_windows([f.normalized_text for f in fragments], budget):
            indices = sorted({i for i, _ in window})
            content = "\n".join(f"{i}: {text}" for i, text in window)
            schema = Boundaries.model_json_schema()
            schema["$defs"]["Boundary"]["properties"]["index"]["enum"] = indices
            schema["properties"]["talks"]["maxItems"] = len(indices)

            def check_boundaries(value: Boundaries, allowed: list[int] = indices) -> list[str]:
                return (
                    []
                    if all(b.index in allowed for b in value.talks)
                    else ["Начало доклада вне текущего окна"]
                )

            found = json_task(
                client,
                profile,
                "Найди начала новых докладов в стенограмме. "
                "Возвращай абсолютный index строки начала, тему и явно названных авторов. "
                "Вопросы и ответы, паузы и реплики ведущего не отдельные доклады. "
                "Если начало доклада не видно, не выдумывай его. Тема по-русски.",
                "Программа:\n" + program[:12000] + "\nСтенограмма:\n" + content,
                Boundaries,
                schema=schema,
                task=f"Разбиение {file['relative_path']}, сегменты {indices[0]}–{indices[-1]}",
                check=check_boundaries,
            )
            for boundary in found.talks:
                boundaries.append(boundary)
        starts = {b.index: b for b in boundaries}
        if fragments and not starts:
            starts[0] = Boundary(
                index=0, title=PurePosixPath(file["relative_path"]).stem + " (тема предварительная)"
            )
        material = next(m for m in existing.files if str(m.source.id) == file["id"])
        previous_talks = [t for t in existing.talks if t.id in material.range_talk_ids]
        used_talks: set[str] = set()
        for index, boundary in enumerate(sorted(starts.values(), key=lambda b: b.index)):
            prior = next(
                (t for t in previous_talks if t.title == boundary.title and t.id not in used_talks),
                None,
            )
            if prior is None and len(previous_talks) == len(starts):
                candidate = previous_talks[index]
                prior = candidate if candidate.id not in used_talks else None
            existing_id = (
                prior.id
                if prior
                else (
                    material.binding.talk_id or material.suggested_talk_id
                    if len(starts) == 1
                    else None
                )
            )
            if existing_id:
                used_talks.add(existing_id)
            known = known_talks.get(existing_id) if existing_id else None
            ordered = sorted(starts)
            end = (
                time_starts[ordered[index + 1]] if index + 1 < len(ordered) else file["duration_ms"]
            )
            proposed.append(
                ProposedTalk(
                    talk_id=existing_id,
                    title=known.title if known and known.title else boundary.title,
                    speakers=(
                        known.speakers
                        if known and known.speakers
                        else [Speaker(name=s) for s in boundary.speakers]
                    ),
                    links=[
                        Link(
                            file_id=file["id"],
                            start_ms=time_starts[boundary.index],
                            end_ms=end,
                        )
                    ],
                    reason="Граница предложена по стенограмме; проверьте начало и конец.",
                )
            )
    for material in existing.files:
        file = next(f for f in files if f["id"] == str(material.source.id))
        if (
            file["kind"] in _RECORDINGS
            or file["id"] in context_files
            or file["id"] in already_linked
        ):
            continue
        if context_ids is None:
            talk_id = material.binding.talk_id or material.suggested_talk_id
        else:
            talk_id = material.binding.talk_id if material.binding.confirmed else None
            if len(material.range_talk_ids) == 1:
                talk_id = material.range_talk_ids[0]
        known = next((t for t in existing.talks if t.id == talk_id), None)
        title = (
            known.title
            if known
            else (material.title_candidates.title if material.title_candidates else None)
        )
        # Доклад из папки заводится в реестре без авторов: тогда берутся авторы титульной
        # страницы, иначе карточка осталась бы без них.
        speakers = (
            known.speakers
            if known and known.speakers
            else [
                Speaker(name=s)
                for s in (material.title_candidates.speakers if material.title_candidates else [])
            ]
        )
        fragments = extracted[file["id"]].fragments
        needs_review = False
        section = known.section if known else None
        if known and not speakers:
            fragments = [f for f in fragments if f.origin is not TextOrigin.VLM]
        if known and not speakers and file["kind"] == FileKind.DOCUMENT:
            first_page = next(
                (
                    fragment.normalized_text
                    for fragment in fragments
                    if isinstance(fragment.locator, PageLocator)
                ),
                "",
            )
            names = dict.fromkeys(
                name
                for line in first_page.splitlines()
                for name in titles.speakers_in_line(line.strip(), explicit_only=True)
            )
            speakers = [Speaker(name=name) for name in names]
        if known is None or (not speakers and fragments and file["kind"] in _CARD_KINDS):
            budget = max(
                4000,
                min(
                    16000,
                    (
                        profile.generation.context_tokens
                        - min(profile.generation.max_tokens, 6000)
                        - 4000
                    ),
                ),
            )
            candidates = (
                [
                    {
                        "index": i,
                        "title": p.title,
                        "speakers": [s.name for s in p.speakers],
                        "excerpt": excerpts[p.links[0].file_id],
                    }
                    for i, p in enumerate(proposed)
                ]
                if known is None
                else []
            )
            material_text = "\n".join(f.normalized_text for f in fragments)[: budget // 2]
            matches = []
            for batch in candidate_windows(candidates, budget // 2):
                allowed = [item["index"] for item in batch]
                content = json.dumps(
                    {
                        "program": program[: budget // 4],
                        "material": material_text,
                        "candidates": batch,
                    },
                    ensure_ascii=False,
                )
                schema = MaterialMatch.model_json_schema()
                schema["properties"]["talk_index"] = {"enum": [None, *allowed]}

                def check_match(value: MaterialMatch, ids: list[int] = allowed) -> list[str]:
                    return [] if value.talk_index in (None, *ids) else ["Неизвестный доклад"]

                try:
                    matched_part = json_task(
                        client,
                        profile,
                        "Определи тему и явно указанных авторов материала. "
                        "Если материал относится к одному из предложенных докладов, укажи "
                        "talk_index и уверенность, сравни содержание независимо от названия файла. "
                        "При неоднозначности talk_index=null. Для программы, вступительного слова "
                        "или организационного совещания укажи role=context. Пленарный доклад с "
                        "содержательной темой остаётся докладом. Если в материале или программе "
                        "указана секция доклада, запиши её точное название в section. "
                        "Не придумывай фамилии. reason объясняет основания.",
                        content,
                        MaterialMatch,
                        schema=schema,
                        task=f"Тема и авторы {file['relative_path']}",
                        check=check_match,
                    )
                except StageError as error:
                    if known is None:
                        raise
                    log.warning("Автор доклада не определён: %s", error.message)
                    break
                matches.append(matched_part)
            if matches:
                matched = max(matches, key=lambda value: value.confidence)
                if known is None:
                    if (
                        context_ids is not None
                        and matched.role == "context"
                        and matched.confidence >= 0.75
                    ):
                        context_ids.add(file["id"])
                        program += "\n" + material_text
                        continue
                    if matched.section and _normalized(matched.section) in _normalized(
                        program + " " + material_text
                    ):
                        section = matched.section
                    title = matched.title or title
                    speakers = [Speaker(name=name) for name in matched.speakers] or speakers
                    text = " ".join(material_text.casefold().split())
                    needs_review = (
                        matched.confidence < 0.85
                        or bool(title and " ".join(title.casefold().split()) not in text)
                        or any(" ".join(s.name.casefold().split()) not in text for s in speakers)
                    )
                    if (
                        matched.talk_index is not None
                        and matched.confidence >= 0.85
                        and matched.talk_index < len(proposed)
                    ):
                        target = proposed[matched.talk_index]
                        if target.section is None:
                            target.section = section
                        target.links.append(Link(file_id=file["id"]))
                        if (
                            needs_review
                            or _conflicting_talk(target, talk_id, speakers)
                            or len(
                                {
                                    m.talk_index
                                    for m in matches
                                    if m.confidence >= 0.85 and m.talk_index is not None
                                }
                            )
                            > 1
                            or not title
                            or title.casefold() != target.title.casefold()
                        ):
                            target.reason = (
                                f"Нужна проверка: связь с материалом {file['relative_path']}."
                            )
                        continue
                else:
                    source = " ".join(material_text.casefold().split())
                    speakers = [
                        Speaker(name=name)
                        for name in dict.fromkeys(matched.speakers)
                        if name.strip() and " ".join(name.casefold().split()) in source
                    ]
        # Attach by known folder first, then exact title; ambiguous files remain separate proposals.
        title = title or PurePosixPath(file["relative_path"]).stem + " (тема предварительная)"
        match = next(
            (
                p
                for p in proposed
                if (
                    p.title.casefold() == title.casefold()
                    and not _conflicting_talk(p, talk_id, speakers)
                )
                or (talk_id is not None and p.talk_id == talk_id)
            ),
            None,
        )
        if match:
            match.links.append(Link(file_id=file["id"]))
            if needs_review:
                match.reason = f"Нужна проверка: источник {file['relative_path']}."
            if not match.speakers:
                match.speakers = speakers
            if match.section is None:
                match.section = section
            if match.talk_id is None:
                match.talk_id = talk_id
        else:
            proposed.append(
                ProposedTalk(
                    talk_id=talk_id,
                    title=title,
                    speakers=speakers,
                    section=section,
                    links=[Link(file_id=file["id"])],
                    reason=(
                        f"Нужна проверка: источник {file['relative_path']}."
                        if needs_review
                        else "Проверьте тему и привязанные материалы."
                    ),
                )
            )

    def model_card(lines: list[str], names: list[str]) -> card.CardHints:
        def ask() -> card.CardAnswer:
            prompt = card.card_prompt(lines, names)
            return json_task(client, profile, card.CARD_INSTRUCTION, prompt, card.CardAnswer)

        key = cache_key("card", card.VERSION, lines, names, profile.model_dump(mode="json"))
        try:
            answer = (
                storage.cached(f"cache/card/{key}.json", card.CardAnswer, ask) if storage else ask()
            )
        except StageError as error:
            # Карточка без модели лучше, чем сорванная подготовка: оператор дополнит её.
            log.warning("Карточка доклада без модели: %s", error.message)
            return card.CardHints()
        return card.grounded(answer, lines, names)

    return [
        with_card(
            talk,
            known_talks.get(talk.talk_id) if talk.talk_id else None,
            _card_lines(talk, by_id, extracted),
            model_card,
        )
        for talk in proposed
    ]


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _conflicting_talk(talk: ProposedTalk, talk_id: str | None, speakers: list[Speaker]) -> bool:
    if talk_id is not None and talk.talk_id is not None:
        return talk_id != talk.talk_id
    known = {" ".join(p.name.casefold().split()) for p in talk.speakers}
    found = {" ".join(p.name.casefold().split()) for p in speakers}
    return bool(known and found and known.isdisjoint(found))


# Карточка берётся из титульной и последней страниц документов и презентаций: на них
# обычно авторы, организации и контакты. Заметки, программа и описания изображений
# моделью не подходят.
_CARD_KINDS = (FileKind.PRESENTATION, FileKind.DOCUMENT, FileKind.IMAGE)


def _card_lines(
    talk: ProposedTalk, files: dict[str, db.Row], extracted: dict[str, Prepared]
) -> list[str]:
    lines: list[str] = []
    for link in talk.links:
        file = files.get(link.file_id)
        if file is None or file["kind"] not in _CARD_KINDS or link.file_id not in extracted:
            continue
        pages: dict[int, list[str]] = {}
        for fragment in slice_fragments(extracted[link.file_id].fragments, link):
            if isinstance(fragment.locator, PageLocator) and fragment.origin is not TextOrigin.VLM:
                pages.setdefault(fragment.locator.page_no, []).append(fragment.normalized_text)
        if not pages:
            continue
        for number in dict.fromkeys((min(pages), max(pages))):
            lines.extend(line for text in pages[number] for line in text.splitlines())
    return lines


def with_card(
    talk: ProposedTalk,
    known: materials.ReviewTalk | None,
    lines: list[str],
    model: Callable[[list[str], list[str]], card.CardHints] | None = None,
) -> ProposedTalk:
    """Дополняет карточку: сначала сведения из реестра (их мог исправить оператор), затем
    найденное шаблонами на страницах, затем — если шаблонов не хватило — моделью.
    Заполненные поля не заменяются."""
    previous = {speaker.name: speaker for speaker in (known.speakers if known else [])}
    names = [speaker.name for speaker in talk.speakers]
    hints = card.card_hints(lines, names, talk.title)
    missing = [
        speaker.name
        for speaker in talk.speakers
        if not speaker.affiliation
        and not (speaker.name in previous and previous[speaker.name].affiliation)
    ]
    if model is not None and card.needs_model(lines, names, missing, hints):
        hints = card.merge(hints, model(lines, names))
    speakers = []
    for speaker in talk.speakers:
        before = previous.get(speaker.name)
        affiliation = (
            speaker.affiliation
            or (before.affiliation if before else None)
            or hints.affiliations.get(speaker.name)
        )
        contacts = (
            speaker.contacts
            or (before.contacts if before else [])
            or hints.speaker_contacts.get(speaker.name, [])
        )
        speakers.append(
            speaker.model_copy(
                update={
                    "affiliation": affiliation,
                    "contacts": contacts,
                    "position": speaker.position or (before.position if before else None),
                }
            )
        )
    attributed = {contact for speaker in speakers for contact in speaker.contacts}
    affiliations = {speaker.affiliation for speaker in speakers if speaker.affiliation}
    organizations = (
        talk.organizations
        or (known.organizations if known else [])
        or [item for item in hints.organizations if item not in affiliations]
    )
    contacts = (
        talk.contacts
        or (known.contacts if known else [])
        or [item for item in hints.contacts if item not in attributed]
    )
    return talk.model_copy(
        update={"speakers": speakers, "organizations": organizations, "contacts": contacts}
    )
