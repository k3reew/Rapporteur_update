"""Шаг А spec §6.7. Факты и дословные цитаты из пакета фрагментов одного доклада.

Вход — только пакет этого доклада (изоляция докладов, spec §3, п. 1). Ответ модели проверяют
схема, валидаторы FactsDraft и проверка меток: ссылка на метку вне пакета уходит на
исправление. Найдена ли цитата, проверяет следующий шаг (verify.py).
"""

import json
import logging
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt

from rapporteur.contracts import (
    FactsArtifact,
    FactsDraft,
    ModelProfile,
    PackedFragment,
    TalkFragmentPack,
)
from rapporteur.contracts.claims import FactDraft
from rapporteur.contracts.enums import FragmentKind, PackBlock
from rapporteur.errors import FailureCode, StageError
from rapporteur.llm.client import LlmClient, Message
from rapporteur.llm.profiles import load_profile
from rapporteur.llm.prompts import Prompt, edition, load_prompt
from rapporteur.llm.structured import ask_structured
from rapporteur.llm.tasks import json_task
from rapporteur.settings import PROMPTS_DIR
from rapporteur.storage import Storage, cache_key
from rapporteur.summarize.pack import bounded_fragment, context_fragments

# Версия шага входит в ключ кэша.
VERSION = "2.5.0"
STEP = "facts"
log = logging.getLogger("rapporteur.facts")

_FACT_LIMITS = {"methods": 3, "results": 5, "conclusions": 3, "listener_remarks": 2}
_SPEAKER_FIELDS = ("methods", "results", "conclusions")
# Метки заметок слушателей (summarize/pack.py): N1, N2…
_NOTE = "N"

_HEADINGS = {
    FragmentKind.SLIDE: "Слайды",
    FragmentKind.DOCUMENT: "Текст выступления",
    FragmentKind.SLIDE_DESCRIPTION: "Описания слайдов",
    FragmentKind.TRANSCRIPT: "Стенограмма",
    FragmentKind.NOTE: "Заметки слушателей",
}


class FactsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # По умолчанию — профиль default из profiles.yaml (spec §9).
    profile: ModelProfile = Field(default_factory=load_profile)
    # Попытки исправить невалидный ответ после первой (backlog P1-06: не больше двух).
    max_repairs: NonNegativeInt = 2
    seed: int = 1


@dataclass(frozen=True)
class FactsBlock:
    heading: str
    fragments: list[PackedFragment]


def extract_facts(
    client: LlmClient,
    pack: TalkFragmentPack,
    config: FactsConfig | None = None,
    storage: Storage | None = None,
) -> FactsArtifact:
    config = config or FactsConfig()
    if not pack.fragments:
        raise StageError(FailureCode.NO_SOURCES, "В материалах доклада нет текста")
    profile = config.profile
    budget = min(
        24000,
        max(1000, (profile.generation.context_tokens - profile.generation.max_tokens - 4000) * 2),
    )
    if sum(len(f.text) + 100 for f in pack.fragments) > budget:
        batches: list[list[PackedFragment]] = [[]]
        size = 0
        for fragment in pack.fragments:
            for start in range(0, len(fragment.text), budget // 2):
                piece = fragment.model_copy(
                    update={"text": fragment.text[start : start + budget // 2]}
                )
                if size + len(piece.text) + 100 > budget:
                    batches.append([])
                    size = 0
                batches[-1].append(piece)
                size += len(piece.text) + 100
        parts = []
        for batch in batches:
            partial = pack.model_copy(update={"fragments": batch})
            digest = cache_key(VERSION, partial.model_dump_json(), config.model_dump_json())
            key = f"cache/facts-parts/{digest}.json"

            def compute(partial: TalkFragmentPack = partial) -> FactsArtifact:
                return extract_facts(client, partial, config)

            parts.append(storage.cached(key, FactsArtifact, compute) if storage else compute())
        # Pairwise reduction keeps input bounded. Original evidence labels/quotes survive.
        while len(parts) > 1:
            merged = []
            for index in range(0, len(parts), 2):
                if index + 1 == len(parts):
                    merged.append(parts[index])
                    continue
                pair = parts[index : index + 2]
                content = "\n".join(item.facts.model_dump_json() for item in pair)
                if len(content) > budget * 2:
                    raise StageError(
                        FailureCode.CONTEXT_OVERFLOW, "Слишком большой пакет фактов для объединения"
                    )
                digest = cache_key(VERSION, content, config.model_dump_json(), "reduce")
                key = f"cache/facts-parts/{digest}.json"
                evidence = [
                    item
                    for part in pair
                    for fact in _all_facts(part.facts)
                    for item in fact.evidence
                ]
                sources = pack.model_copy(update={"fragments": context_fragments(pack, evidence)})

                def reduce_pair(
                    content: str = content,
                    sources: TalkFragmentPack = sources,
                    first: FactsArtifact = pair[0],
                ) -> FactsArtifact:
                    if edition(profile.prompt_version) >= 2:
                        if not sources.fragments:
                            return first
                        return _review_facts(client, sources, config, content)
                    facts = json_task(
                        client,
                        profile,
                        "Объедини факты частей одного доклада в краткий набор основных фактов. "
                        "Сохраняй дословные цитаты и метки источников. Не добавляй новых фактов. "
                        "Сохрани ключевые результаты, числа, методы и выводы, убери повторы.",
                        content,
                        FactsDraft,
                    )
                    if unknown_labels(facts, pack):
                        raise StageError(
                            FailureCode.LLM_INVALID_OUTPUT,
                            "Неизвестная метка при объединении фактов",
                        )
                    return FactsArtifact(
                        talk_id=pack.talk_id,
                        profile=profile.name,
                        prompt_version=profile.prompt_version,
                        facts=facts,
                        attempts=1,
                    )

                merged.append(
                    storage.cached(key, FactsArtifact, reduce_pair) if storage else reduce_pair()
                )
            parts = merged
        return parts[0]
    prompt = load_prompt(STEP, profile.prompt_version, PROMPTS_DIR)
    result = ask_structured(
        client,
        profile,
        prompt,
        facts_messages(prompt, pack),
        FactsDraft,
        think=profile.generation.reasoning,
        max_repairs=config.max_repairs,
        seed=config.seed,
        check=lambda facts: _facts_problems(facts, pack),
        schema=_facts_schema(pack),
        prepare=normalize_facts,
    )
    if edition(profile.prompt_version) >= 2:
        reviewed = _review_facts(client, pack, config, result.value.model_dump_json())
        return reviewed.model_copy(update={"attempts": result.attempts + reviewed.attempts})
    return FactsArtifact(
        talk_id=pack.talk_id,
        profile=profile.name,
        prompt_version=prompt.version,
        facts=result.value,
        attempts=result.attempts,
    )


def _facts_schema(pack: TalkFragmentPack) -> dict[str, Any]:
    """Схема ответа для грамматики Ollama. Метки опор разведены по полям: реплики слушателей
    ссылаются только на заметки N, утверждения докладчика — на остальные фрагменты. Иначе
    модель кладёт вопросы из зала со ссылкой на стенограмму в listener_remarks, контракт
    отвергает ответ, и шаг падает после всех исправлений."""
    labels = list(pack.by_label())
    notes = [label for label in labels if label.startswith(_NOTE)]
    speech = [label for label in labels if not label.startswith(_NOTE)] or labels
    schema = FactsDraft.model_json_schema()
    definitions = schema["$defs"]
    definitions["EvidenceDraft"]["properties"]["label"]["enum"] = speech
    for field, limit in _FACT_LIMITS.items():
        schema["properties"][field]["maxItems"] = limit
    definitions["FactDraft"]["properties"]["text"]["maxLength"] = 350
    definitions["FactDraft"]["properties"]["evidence"]["maxItems"] = 2
    definitions["EvidenceDraft"]["properties"]["quote"]["maxLength"] = 240
    if notes:
        note = deepcopy(definitions["EvidenceDraft"])
        note["title"] = "NoteEvidenceDraft"
        note["properties"]["label"]["enum"] = notes
        remark = deepcopy(definitions["FactDraft"])
        remark["title"] = "ListenerRemarkDraft"
        remark["properties"]["evidence"]["items"] = {"$ref": "#/$defs/NoteEvidenceDraft"}
        definitions["NoteEvidenceDraft"] = note
        definitions["ListenerRemarkDraft"] = remark
        schema["properties"]["listener_remarks"]["items"] = {"$ref": "#/$defs/ListenerRemarkDraft"}
    else:
        # Заметок слушателей нет: реплик слушателей быть не может.
        schema["properties"]["listener_remarks"]["maxItems"] = 0
    definitions = schema.pop("$defs")
    absent = deepcopy(schema)
    present = deepcopy(schema)
    absent["properties"]["conclusions"]["maxItems"] = 0
    absent["properties"]["conclusions_absent_reason"] = {"type": "string", "minLength": 1}
    present["properties"]["conclusions"]["minItems"] = 1
    present["properties"]["conclusions_absent_reason"] = {"type": "null"}
    return {"title": schema["title"], "$defs": definitions, "oneOf": [absent, present]}


def normalize_facts(raw: Any) -> Any:
    """Страховка на случай, если грамматика не применилась: то, что код исправляет
    однозначно, исправляется до проверки контракта, а не отправляет ответ на повтор.

    Опоры на заметки N убираются из утверждений докладчика, опоры не на заметки — из
    реплик слушателей; утверждение без оставшихся опор отбрасывается. Причина отсутствия
    выводов согласуется со списком выводов."""
    if not isinstance(raw, dict):
        return raw
    value = dict(raw)
    dropped = 0

    def keep(fact: Any, notes: bool) -> Any:
        nonlocal dropped
        if not isinstance(fact, dict) or not isinstance(fact.get("evidence"), list):
            return fact
        evidence = [
            item
            for item in fact["evidence"]
            if not isinstance(item, dict) or str(item.get("label", "")).startswith(_NOTE) == notes
        ]
        if len(evidence) == len(fact["evidence"]):
            return fact
        if not evidence:
            dropped += 1
            return None
        return {**fact, "evidence": evidence}

    for field, notes in (*((name, False) for name in _SPEAKER_FIELDS), ("listener_remarks", True)):
        items = value.get(field)
        if isinstance(items, list):
            value[field] = [fact for item in items if (fact := keep(item, notes)) is not None]
    if isinstance(value.get("problem"), dict):
        value["problem"] = keep(value["problem"], False)
    conclusions = value.get("conclusions")
    if isinstance(conclusions, list):
        if conclusions:
            value["conclusions_absent_reason"] = None
        elif not value.get("conclusions_absent_reason"):
            value["conclusions_absent_reason"] = "Явные выводы автора в материалах не найдены"
    if dropped:
        log.info("Факты: отброшено %s утверждений с опорами не своего вида", dropped)
    return value


def _facts_problems(facts: FactsDraft, pack: TalkFragmentPack) -> list[str]:
    problems = unknown_labels(facts, pack)
    problems.extend(
        f"В {field} допустимо не более {limit} фактов"
        for field, limit in _FACT_LIMITS.items()
        if len(getattr(facts, field)) > limit
    )
    for fact in _all_facts(facts):
        if len(fact.text) > 350 or len(fact.evidence) > 2:
            problems.append("Факт должен содержать до 350 символов и до двух опор")
        if any(len(item.quote) > 240 for item in fact.evidence):
            problems.append("Дословная цитата должна содержать до 240 символов")
    return problems


def _review_facts(
    client: LlmClient, pack: TalkFragmentPack, config: FactsConfig, draft: str
) -> FactsArtifact:
    profile = config.profile
    prompt = load_prompt("facts-review", profile.prompt_version, PROMPTS_DIR)
    parts = [FactsDraft.model_validate_json(line) for line in draft.splitlines()]
    evidence = [item for part in parts for fact in _all_facts(part) for item in fact.evidence]
    if len(parts) > 1:
        draft = (
            "Объедини факты всех частей одного доклада. Сохрани существенные поддержанные "
            "сведения каждой части, убери повторы и уточни формулировки по источникам. "
            "Каждый элемент массива содержит отдельную часть.\n"
            "Части фактов в JSON-массиве.\n"
            + json.dumps([part.model_dump(mode="json") for part in parts], ensure_ascii=False)
        )
    result = ask_structured(
        client,
        profile,
        prompt,
        [
            Message("system", prompt.render("system")),
            Message(
                "user",
                prompt.render(
                    "user",
                    draft=draft,
                    fragments=[bounded_fragment(fragment, evidence) for fragment in pack.fragments],
                ),
            ),
        ],
        FactsDraft,
        think=profile.generation.reasoning,
        max_repairs=config.max_repairs,
        seed=config.seed,
        check=lambda facts: _facts_problems(facts, pack),
        schema=_facts_schema(pack),
        prepare=normalize_facts,
    )
    return FactsArtifact(
        talk_id=pack.talk_id,
        profile=profile.name,
        prompt_version=profile.prompt_version,
        facts=result.value,
        attempts=result.attempts,
    )


def facts_messages(prompt: Prompt, pack: TalkFragmentPack) -> list[Message]:
    blocks = [
        FactsBlock(heading, fragments)
        for heading, fragments in _blocks(pack)
        if fragments  # пустые блоки не показываются модели
    ]
    return [
        Message("system", prompt.render("system")),
        Message("user", prompt.render("user", blocks=blocks)),
    ]


def _blocks(pack: TalkFragmentPack) -> Iterator[tuple[str, list[PackedFragment]]]:
    for kind, heading in _HEADINGS.items():
        main = [f for f in pack.fragments if f.kind is kind and f.block is PackBlock.MAIN]
        yield heading, main
    questions = [f for f in pack.fragments if f.block is PackBlock.QA]
    yield "Вопросы и ответы", questions


def unknown_labels(facts: FactsDraft, pack: TalkFragmentPack) -> list[str]:
    known = pack.by_label()
    problems: list[str] = []
    for fact in _all_facts(facts):
        problems.extend(
            f"метки {item.label} нет в материалах доклада (факт «{fact.text}»)"
            for item in fact.evidence
            if item.label not in known
        )
    return problems


def _all_facts(facts: FactsDraft) -> Iterator[FactDraft]:
    if facts.problem is not None:
        yield facts.problem
    yield from facts.methods
    yield from facts.results
    yield from facts.conclusions
    yield from facts.listener_remarks
