"""Шаги В и Г spec §6.7 со смысловым критиком для промптов v2 и v3.

Модель пишет текст только из утверждений, прошедших сверку. Код проверяет ссылки на
утверждения, число предложений, числа и запрещённые слова; при нарушениях сводка пишется
заново с замечаниями, до max_attempts раз. После этого она получает статус needs_attention:
в неё попадает лучшая попытка без строк с недопустимыми ссылками, а решение за человеком.
Статус доклада меняет обработка (processing.py): доклад становится processed.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator

from rapporteur.contracts import (
    Claim,
    Evidence,
    FactsArtifact,
    ModelProfile,
    Summary,
    SummaryDraft,
    SummaryLine,
    TalkFragmentPack,
    TalkSummaryBundle,
)
from rapporteur.contracts.claims import CheckResult
from rapporteur.contracts.enums import CheckName, ClaimType, SummaryStatus
from rapporteur.llm.client import LlmClient, Message
from rapporteur.llm.profiles import load_profile
from rapporteur.llm.prompts import edition, load_prompt
from rapporteur.llm.structured import ask_structured
from rapporteur.settings import PROMPTS_DIR
from rapporteur.summarize.checks import (
    CLAIM_NAMES,
    SummaryChecksConfig,
    check_summary,
    required_lines,
    sentence_range,
    usable_claims,
)
from rapporteur.summarize.grounding import review_summary
from rapporteur.summarize.pack import context_fragments
from rapporteur.summarize.terms import Definition, abbreviations, find_definitions

# Версия шага входит в ключ кэша.
VERSION = "2.1.0"
STEP = "summary"

_SUMMARY_NAMESPACE = uuid.UUID("9a4e2c71-3b5d-4f8e-a6c0-7d1b2e3f4a5c")
_KIND = {
    ClaimType.PROBLEM: "задача",
    ClaimType.METHOD: "метод",
    ClaimType.RESULT: "результат",
    ClaimType.CONCLUSION: "вывод",
}


class SummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # По умолчанию — профиль default из profiles.yaml (spec §9).
    profile: ModelProfile = Field(default_factory=load_profile)
    checks: SummaryChecksConfig = SummaryChecksConfig()
    # Сколько раз сводка пишется с замечаниями проверок, прежде чем уйти человеку.
    max_attempts: PositiveInt = 3
    # Исправления формы ответа внутри одной попытки.
    max_repairs: NonNegativeInt = 1
    seed: int = 1

    @model_validator(mode="after")
    def reader_defaults(self) -> "SummaryConfig":
        if edition(self.profile.prompt_version) >= 3 and "checks" not in self.model_fields_set:
            object.__setattr__(
                self, "checks", SummaryChecksConfig(min_sentences=6, max_sentences=10)
            )
        return self


@dataclass(frozen=True)
class ClaimLine:
    key: str
    kind: str
    text: str
    evidence: list[Evidence]


@dataclass(frozen=True)
class Written:
    status: SummaryStatus
    sentences: list[SummaryLine]
    conclusions: list[SummaryLine]
    checks: list[CheckResult]
    attempts: int


def write_summary(
    client: LlmClient,
    config: SummaryConfig,
    title: str | None,
    claims: Sequence[Claim],
    pack: TalkFragmentPack,
) -> Written:
    usable = usable_claims(claims)
    if not usable:
        missing = CheckResult(
            check=CheckName.CLAIM_LINKS,
            passed=False,
            attempt=1,
            details="нет утверждений, подтверждённых цитатами",
        )
        return Written(SummaryStatus.NEEDS_ATTENTION, [], [], [missing], 0)

    profile = config.profile
    prompt = load_prompt(STEP, profile.prompt_version, PROMPTS_DIR)
    low, high = sentence_range(config.checks, claims)
    # Утверждения идут в порядке важности их типов (summary.yaml, priorities).
    ordered = sorted(usable, key=lambda claim: config.checks.rank(claim.type))
    lines = [
        ClaimLine(claim.key, _KIND[claim.type], claim.text, claim.evidence) for claim in ordered
    ]
    evidence = [item for claim in usable for item in claim.evidence]
    # Расшифровки сокращений из материалов; без расшифровки сокращение в текст не входит.
    glossary: dict[str, Definition] = {}
    unexplained: list[str] = []
    if config.checks.explain_abbreviations:
        used = [
            item
            for item in abbreviations(" ".join(claim.text for claim in ordered))
            if item not in config.checks.known_abbreviations
        ]
        glossary = find_definitions(pack, used)
        unexplained = [item for item in used if item not in glossary]
    messages = [
        Message(
            "system",
            prompt.render(
                "system",
                min_sentences=low,
                max_sentences=high,
                priorities=[CLAIM_NAMES[item] for item in config.checks.priorities],
                required=required_lines(config.checks, claims),
                explain_abbreviations=config.checks.explain_abbreviations,
                known_abbreviations=config.checks.known_abbreviations,
            ),
        ),
        Message(
            "user",
            prompt.render(
                "user",
                title=title,
                claims=lines,
                has_conclusions=any(claim.type is ClaimType.CONCLUSION for claim in usable),
                fragments=context_fragments(pack, evidence),
                glossary=list(glossary.values()),
                unexplained=unexplained,
            ),
        ),
    ]
    checks: list[CheckResult] = []
    best: tuple[tuple[bool, bool, int], SummaryDraft] | None = None
    for attempt in range(1, config.max_attempts + 1):
        result = ask_structured(
            client,
            profile,
            prompt,
            messages,
            SummaryDraft,
            think=profile.generation.reasoning,
            max_repairs=config.max_repairs,
            seed=config.seed,
        )
        draft = _punctuated(result.value)
        current = check_summary(draft, claims, pack, config.checks, attempt)
        if edition(profile.prompt_version) >= 2 and all(check.passed for check in current):
            current.extend(
                review_summary(
                    client,
                    profile,
                    draft,
                    claims,
                    pack,
                    attempt=attempt,
                    seed=config.seed,
                    glossary={key: item.label for key, item in glossary.items()},
                )
            )
        checks.extend(current)
        failed = [check for check in current if not check.passed]
        if not failed:
            return Written(SummaryStatus.READY, draft.sentences, draft.conclusions, checks, attempt)
        rank = (
            any(check.check is CheckName.CLAIM_LINKS and not check.passed for check in current),
            any(check.check is CheckName.SENTENCE_COUNT and not check.passed for check in current),
            len(failed),
        )
        if best is None or rank <= best[0]:
            best = (rank, draft)
        problems = [check.details or check.check.value for check in failed]
        messages = [*result.messages, Message("user", prompt.render("repair", problems=problems))]

    assert best is not None
    draft = best[1]
    return Written(
        SummaryStatus.NEEDS_ATTENTION,
        _linked(draft.sentences, claims, conclusions=False),
        _linked(draft.conclusions, claims, conclusions=True),
        checks,
        config.max_attempts,
    )


_TERMINAL = ".!?…"
_CLOSING = '»")]'


def _terminated(text: str) -> str:
    """Строка сводки — законченное предложение. Модель иногда опускает точку, и в справке
    предложения слились бы в одно; точка ставится после закрывающих кавычек и скобок."""
    stripped = text.rstrip()
    core = stripped.rstrip(_CLOSING)
    if not core or core[-1] in _TERMINAL:
        return stripped
    return stripped + "."


def _punctuated(draft: SummaryDraft) -> SummaryDraft:
    def fix(lines: Sequence[SummaryLine]) -> list[SummaryLine]:
        return [line.model_copy(update={"text": _terminated(line.text)}) for line in lines]

    return draft.model_copy(
        update={"sentences": fix(draft.sentences), "conclusions": fix(draft.conclusions)}
    )


def _linked(
    lines: Sequence[SummaryLine], claims: Sequence[Claim], *, conclusions: bool
) -> list[SummaryLine]:
    """Строка остаётся, только если все её ссылки допустимы: текст, опирающийся на
    отклонённое утверждение, не сохраняется даже частично."""
    allowed = {
        claim.key: claim
        for claim in usable_claims(claims)
        if not conclusions or claim.type is ClaimType.CONCLUSION
    }
    return [line for line in lines if all(key in allowed for key in line.claim_ids)]


def make_summary(
    seed: str,
    pack: TalkFragmentPack,
    facts: FactsArtifact,
    claims: Sequence[Claim],
    written: Written,
    *,
    profile: str,
    prompt_version: str,
) -> Summary:
    """Версию сводки ведёт БД; конвейер выдаёт первую. Пакет и сводка проверяются вместе
    (TalkSummaryBundle) до публикации."""
    summary = Summary(
        id=uuid.uuid5(_SUMMARY_NAMESPACE, seed),
        talk_id=pack.talk_id,
        version=1,
        status=written.status,
        profile=profile,
        prompt_versions={"facts": facts.prompt_version, STEP: prompt_version},
        input_revision_ids=[fragment.revision_id for fragment in pack.fragments],
        facts=facts.facts,
        claims=list(claims),
        sentences=written.sentences,
        conclusions=written.conclusions,
        checks=written.checks,
        human_edit=False,
        created_at=datetime.now(UTC),
    )
    TalkSummaryBundle(pack=pack, summary=summary)
    return summary
