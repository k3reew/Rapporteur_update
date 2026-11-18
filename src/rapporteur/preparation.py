"""Cached multimodal extraction and reviewable conference segmentation."""

import base64
import json
import logging
import sqlite3
import urllib.request
from collections.abc import Callable, Iterator
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rapporteur import db, materials, model_choice, settings
from rapporteur.contracts import FragmentDraft, ModelProfile, Speaker
from rapporteur.contracts.base import NonEmptyStr
from rapporteur.contracts.enums import FileKind, FragmentKind, TextOrigin
from rapporteur.contracts.fragments import PageLocator, TimeLocator
from rapporteur.errors import FailureCode, StageError, UserError
from rapporteur.ingest import card, documents, notes, titles
from rapporteur.llm.client import LlmClient
from rapporteur.llm.tasks import json_task
from rapporteur.storage import Storage, blob_key, cache_key

log = logging.getLogger("rapporteur.preparation")

VERSION = "2.0.0"
_RECORDINGS = (FileKind.AUDIO, FileKind.VIDEO)
_UNTITLED = "Тема не установлена"
ASR_DISABLED = "Распознавание речи выключено (ASR_MODE): запись не расшифрована"
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


class ASRSegment(BaseModel):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=1)
    text: str


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


class ProposalWrite(BaseModel):
    version: int = Field(ge=1)
    talks: list[ProposedTalk] = Field(max_length=500)


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


def _asr_revision(file: db.Row) -> str:
    import os
    from pathlib import Path

    from rapporteur.asr_models import resolve_asr_model

    if file["kind"] not in _RECORDINGS:
        return ""
    root = Path(os.environ.get("ASR_MODEL_DIR", "models/asr"))
    manifest = resolve_asr_model(root) / "manifest.json"
    return manifest.read_text() if manifest.is_file() else ""


def prepared_key(file: db.Row, profile_revision: str, *, asr_revision: str | None = None) -> str:
    if asr_revision is None:
        asr_revision = _asr_revision(file)
    profile_revision += asr_revision
    return (
        f"cache/prepared/{cache_key(VERSION, file['sha256'], file['kind'], profile_revision)}.json"
    )


def extract(
    file: db.Row,
    storage: Storage,
    client: LlmClient,
    profile: ModelProfile,
) -> Prepared:
    asr_revision = _asr_revision(file)
    key = prepared_key(file, profile.model_dump_json(), asr_revision=asr_revision)
    if file["kind"] in _RECORDINGS and not settings.ASR_ENABLED and not storage.exists(key):
        # Не кэшируется: после включения ASR запись расшифруется. Готовая расшифровка
        # из кэша используется и без ASR.
        return Prepared(fragments=[], warnings=[ASR_DISABLED])

    def compute() -> Prepared:
        sha, kind = file["sha256"], file["kind"]
        if kind in (FileKind.AUDIO, FileKind.VIDEO):
            try:
                request = urllib.request.Request(
                    settings.ASR_URL + "/transcribe",
                    json.dumps({"sha256": sha}).encode(),
                    {"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=14400) as answer:
                    result = json.load(answer)
                if asr_revision and result.get("revision") != asr_revision:
                    raise ValueError("Несовпадение ревизии ASR: перезапустите app и ASR вместе")
                segments = result["segments"]
            except (OSError, ValueError, KeyError) as error:
                raise StageError(FailureCode.LLM_UNAVAILABLE, f"ASR недоступен: {error}") from error
            # Merge segments starting in the same second to keep transcript labels unique.
            merged: dict[int, ASRSegment] = {}
            for raw in segments:
                segment = ASRSegment.model_validate(raw)
                bucket = segment.start_ms // 1000
                if bucket in merged:
                    merged[bucket].text += " " + segment.text
                    merged[bucket].end_ms = segment.end_ms
                else:
                    merged[bucket] = segment
            fragments = []
            for segment in merged.values():
                locator = TimeLocator(kind="time", start_ms=segment.start_ms, end_ms=segment.end_ms)
                fragments.append(
                    FragmentDraft(
                        kind=FragmentKind.TRANSCRIPT,
                        source_sha256=sha,
                        locator=locator,
                        raw_text=segment.text,
                        normalized_text=segment.text,
                        origin=TextOrigin.ASR,
                        engine_confidence=None,
                    )
                )
            return Prepared(fragments=fragments)
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
        return Prepared(fragments=fragments, images=images, warnings=warnings)

    return storage.cached(key, Prepared, compute)


def queue(conn: sqlite3.Connection, conference_id: str) -> None:
    from rapporteur.departments import require_regulations

    require_regulations(conn)
    materials.require_conference(conn, conference_id)
    row = db.one(conn, "SELECT job FROM preparations WHERE conference_id = ?", conference_id)
    if row and row["job"] in ("queued", "running"):
        raise UserError("Подготовка уже выполняется", 409)
    conn.execute(
        "INSERT INTO preparations(conference_id, job) VALUES (?, 'queued') "
        "ON CONFLICT(conference_id) DO UPDATE SET job='queued', error=NULL, version=version+1",
        (conference_id,),
    )


def state(conn: sqlite3.Connection, conference_id: str) -> dict[str, object]:
    materials.require_conference(conn, conference_id)
    row = db.one(conn, "SELECT * FROM preparations WHERE conference_id = ?", conference_id)
    warnings = _warnings(conn, conference_id)
    if not row:
        return {"version": 0, "job": None, "error": None, "talks": [], "warnings": warnings}
    proposal = json.loads(row["proposal"])
    if row["job"] == "confirmed":
        talks = {talk["id"]: talk for talk in db.conference_talks(conn, conference_id)}
        for item in proposal:
            if (talk := talks.get(item.get("talk_id"))) is not None:
                item.update(_card_fields(talk))
    return {
        "version": row["version"],
        "job": row["job"],
        "error": row["error"],
        "talks": proposal,
        "warnings": warnings,
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
        "job=CASE WHEN job='running' THEN 'queued' ELSE job END WHERE conference_id=?",
        (db.dumps(proposal), row["conference_id"]),
    )


def _warnings(conn: sqlite3.Connection, conference_id: str) -> list[str]:
    if settings.ASR_ENABLED:
        return []
    recordings = conn.execute(
        "SELECT count(*) FROM files WHERE conference_id = ? AND kind IN (?, ?)",
        (conference_id, *_RECORDINGS),
    ).fetchone()[0]
    if not recordings:
        return []
    return [
        f"Распознавание речи выключено (ASR_MODE), записей в конференции: {recordings}. "
        "Доклады по нерасшифрованным записям не предлагаются, их текст не входит в сводки. "
        "Записи можно привязать вручную или включить распознавание."
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
) -> None:
    materials.require_conference(conn, conference_id)
    current = db.one(conn, "SELECT * FROM preparations WHERE conference_id=?", conference_id)
    if (
        current is None
        or current["version"] != data.version
        or current["job"] not in ("done", "confirmed")
    ):
        raise UserError("Разбиение изменилось или ещё не готово: обновите страницу", 409)
    files = {
        r["id"]: r
        for r in conn.execute("SELECT * FROM files WHERE conference_id=?", (conference_id,))
    }
    talks = {r["id"]: r for r in db.conference_talks(conn, conference_id)}
    intervals: dict[str, list[tuple[int, int]]] = {}
    assigned = set()
    for item in data.talks:
        if item.talk_id and (item.talk_id not in talks or item.talk_id in assigned):
            raise UserError("Доклад отсутствует или повторяется")
        assigned.add(item.talk_id)
        for link in item.links:
            file = files.get(link.file_id)
            if file is None or file["kind"] == FileKind.PROGRAM:
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
    previous = conn.execute(
        "SELECT DISTINCT material_links.talk_id FROM material_links JOIN files ON files.id=file_id "
        "WHERE conference_id=? UNION SELECT talk_id FROM files WHERE "
        "conference_id=? AND talk_id IS NOT NULL",
        (conference_id, conference_id),
    ).fetchall()
    conn.execute(
        "DELETE FROM material_links WHERE file_id IN (SELECT id FROM files WHERE conference_id=?)",
        (conference_id,),
    )
    changed = {r["talk_id"] for r in previous}
    conn.execute(
        "UPDATE files SET confirmed=1, talk_id=NULL WHERE conference_id=?", (conference_id,)
    )
    saved = []
    for item in data.talks:
        talk_id = item.talk_id or materials.add_talk(conn, conference_id, item.title, None)
        changed.add(talk_id)
        title: str | None = item.title
        if title == _UNTITLED and item.talk_id and talks[item.talk_id]["title"] is None:
            title = None
        conn.execute(
            "UPDATE talks SET title=?, speakers=?, organizations=?, contacts=? WHERE id=?",
            (
                title,
                db.dumps([s.model_dump(mode="json") for s in item.speakers]),
                db.dumps(item.organizations),
                db.dumps(item.contacts),
                talk_id,
            ),
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
        "job='confirmed' WHERE conference_id=?",
        (db.dumps(saved), conference_id),
    )


def propose(
    files: list[db.Row],
    extracted: dict[str, Prepared],
    existing: materials.Materials,
    client: LlmClient,
    profile: ModelProfile,
    storage: Storage | None = None,
) -> list[ProposedTalk]:
    proposed: list[ProposedTalk] = []
    known_talks = {talk.id: talk for talk in existing.talks}
    program = "\n".join(
        f.normalized_text
        for file in files
        if file["kind"] == FileKind.PROGRAM
        for f in extracted[file["id"]].fragments
    )
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
        if file["kind"] in (FileKind.AUDIO, FileKind.VIDEO, FileKind.PROGRAM):
            continue
        talk_id = material.binding.talk_id or material.suggested_talk_id
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
                    {"index": i, "title": p.title, "speakers": [s.name for s in p.speakers]}
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
                        "talk_index и уверенность. При неоднозначности talk_index=null. "
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
                    title = matched.title or title
                    speakers = [Speaker(name=name) for name in matched.speakers] or speakers
                    if (
                        matched.talk_index is not None
                        and matched.confidence >= 0.85
                        and matched.talk_index < len(proposed)
                    ):
                        proposed[matched.talk_index].links.append(Link(file_id=file["id"]))
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
                if p.title.casefold() == title.casefold()
                or (talk_id is not None and p.talk_id == talk_id)
            ),
            None,
        )
        if match:
            match.links.append(Link(file_id=file["id"]))
            if not match.speakers:
                match.speakers = speakers
            if match.talk_id is None:
                match.talk_id = talk_id
        else:
            proposed.append(
                ProposedTalk(
                    talk_id=talk_id,
                    title=title,
                    speakers=speakers,
                    links=[Link(file_id=file["id"])],
                    reason="Проверьте тему и привязку к записи.",
                )
            )
    by_id = {file["id"]: file for file in files}

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
            speaker.model_copy(update={"affiliation": affiliation, "contacts": contacts})
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
