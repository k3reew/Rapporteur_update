"""Смысловая проверка предложений по их привязанным источникам, отдельно от поиска цитат."""

import json
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from rapporteur.contracts import Claim, ModelProfile, SummaryDraft, TalkFragmentPack
from rapporteur.contracts.claims import CheckResult
from rapporteur.contracts.enums import CheckName
from rapporteur.llm.client import LlmClient, Message
from rapporteur.llm.prompts import edition, load_prompt
from rapporteur.llm.structured import ask_structured
from rapporteur.summarize.pack import context_fragments
from rapporteur.summarize.terms import abbreviations


class LineDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: str
    supported: bool
    reason: str = Field(min_length=1, max_length=240)


class GroundingReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decisions: list[LineDecision]


class ReaderReview(GroundingReview):
    reader_passed: bool
    reader_issues: list[str] = Field(max_length=6)


def review_summary(
    client: LlmClient,
    profile: ModelProfile,
    draft: SummaryDraft,
    claims: Sequence[Claim],
    pack: TalkFragmentPack,
    *,
    attempt: int,
    seed: int,
    glossary: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """glossary — метка фрагмента с расшифровкой сокращения: если строка расшифровывает
    сокращение, критик видит и этот фрагмент. С редакции v3 критик также оценивает
    понятность текста новому читателю (READER_CONTEXT)."""
    by_key = {claim.key: claim for claim in claims}
    fragments = {fragment.label: fragment for fragment in pack.fragments}
    lines = []
    expected: list[str] = []
    for prefix, items in (("s", draft.sentences), ("c", draft.conclusions)):
        for number, line in enumerate(items, 1):
            expected.append(f"{prefix}{number}")
            evidence = [item for key in line.claim_ids for item in by_key[key].evidence]
            sources = [
                {"label": fragment.label, "text": fragment.text}
                for fragment in context_fragments(pack, evidence)
            ]
            for abbreviation in abbreviations(line.text):
                label = (glossary or {}).get(abbreviation)
                if label and label in fragments and all(s["label"] != label for s in sources):
                    sources.append({"label": label, "text": fragments[label].text})
            lines.append({"line": f"{prefix}{number}", "text": line.text, "sources": sources})
    output = ReaderReview if edition(profile.prompt_version) >= 3 else GroundingReview
    schema = output.model_json_schema()
    if output is ReaderReview:
        schema["properties"]["reader_issues"]["items"]["maxLength"] = 240
    schema["$defs"]["LineDecision"]["properties"]["line"]["enum"] = expected
    schema["properties"]["decisions"]["minItems"] = len(expected)
    schema["properties"]["decisions"]["maxItems"] = len(expected)
    prompt = load_prompt("critic", profile.prompt_version)

    def complete(review: GroundingReview) -> list[str]:
        if isinstance(review, ReaderReview) and (
            review.reader_passed == bool(review.reader_issues)
            or any(len(issue) > 240 for issue in review.reader_issues)
        ):
            return [
                "Для непонятного текста перечисли краткие замечания, для понятного список пуст."
            ]
        found = [decision.line for decision in review.decisions]
        if sorted(found) == sorted(expected):
            return []
        return ["Нужно ровно одно решение для каждой строки: " + ", ".join(expected)]

    reviewed = ask_structured(
        client,
        profile,
        prompt,
        [
            Message("system", prompt.render("system")),
            Message("user", prompt.render("user", lines=json.dumps(lines, ensure_ascii=False))),
        ],
        output,
        think=False,
        max_repairs=1,
        seed=seed,
        check=complete,
        schema=schema,
    )
    problems = [
        decision.line + ": " + decision.reason
        for decision in reviewed.value.decisions
        if not decision.supported
    ]
    checks = [
        CheckResult(
            check=CheckName.CRITIC,
            passed=not problems,
            attempt=attempt,
            details="; ".join(problems) if problems else None,
        )
    ]
    if isinstance(reviewed.value, ReaderReview):
        checks.append(
            CheckResult(
                check=CheckName.READER_CONTEXT,
                passed=reviewed.value.reader_passed,
                attempt=attempt,
                details="; ".join(reviewed.value.reader_issues) or None,
            )
        )
    return checks
