"""Смысловая проверка предложений по их привязанным источникам, отдельно от поиска цитат."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rapporteur.contracts import Claim, ModelProfile, SummaryDraft, TalkFragmentPack
from rapporteur.contracts.claims import CheckResult
from rapporteur.contracts.enums import CheckName
from rapporteur.errors import FailureCode, StageError
from rapporteur.llm.client import LlmClient, Message
from rapporteur.llm.prompts import edition, load_prompt
from rapporteur.llm.structured import ask_structured
from rapporteur.summarize.pack import context_fragments
from rapporteur.summarize.terms import abbreviations, definition_at


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
    definitions = {}
    for abbreviation, label in (glossary or {}).items():
        if label in fragments and (match := definition_at(fragments[label].text, abbreviation)):
            definitions[abbreviation] = (label, match[1])
    lines: list[tuple[str, str, list[dict[str, str]]]] = []
    for prefix, items in (("s", draft.sentences), ("c", draft.conclusions)):
        for number, line in enumerate(items, 1):
            evidence = [item for key in line.claim_ids for item in by_key[key].evidence]
            anchors: dict[str, list[tuple[int, int]]] = {}
            for abbreviation in abbreviations(line.text):
                if abbreviation in definitions:
                    label, span = definitions[abbreviation]
                    anchors.setdefault(label, []).append(span)
            sources = [
                {"label": fragment.label, "text": fragment.text}
                for fragment in context_fragments(pack, evidence, anchors=anchors)
            ]
            lines.append((f"{prefix}{number}", line.text, sources))

    output = ReaderReview if edition(profile.prompt_version) >= 3 else GroundingReview
    prompt = load_prompt("critic", profile.prompt_version)
    system = prompt.render("system")
    # Ответ ограничен краткими причинами по строкам и шестью замечаниями читателя.
    max_tokens = min(profile.generation.max_tokens, 1024 + 256 * len(lines))
    generation = profile.generation.model_copy(update={"max_tokens": max_tokens})
    critic_profile = profile.model_copy(update={"generation": generation})
    # Кириллица расходует около 3 символов на токен. Оставляем место для ответа,
    # предыдущей попытки и инструкции исправления.
    budget = 3 * (generation.context_tokens - 2 * generation.max_tokens - 256)

    def request(
        rows: Sequence[tuple[str, str, list[dict[str, str]]]],
        reader_context: str = "",
    ) -> tuple[list[Message], dict[str, Any]]:
        schema = output.model_json_schema()
        if output is ReaderReview:
            schema["properties"]["reader_issues"]["items"]["maxLength"] = 240
        schema["$defs"]["LineDecision"]["properties"]["line"]["enum"] = [row[0] for row in rows]
        schema["properties"]["decisions"]["minItems"] = len(rows)
        schema["properties"]["decisions"]["maxItems"] = len(rows)
        payload = [{"line": key, "text": text, "sources": sources} for key, text, sources in rows]
        user = prompt.render("user", lines=json.dumps(payload, ensure_ascii=False))
        system_text = system
        if reader_context:
            system_text += (
                "\nОцени понятность по полному тексту сводки. "
                "Решения о поддержке дай только по строкам с источниками."
            )
            user = f"Полный текст сводки: {reader_context}\nСтроки с источниками: {user}"
        return [Message("system", system_text), Message("user", user)], schema

    def fits(messages: list[Message], schema: dict[str, Any]) -> bool:
        chars = sum(len(message.content) for message in messages) + len(json.dumps(schema))
        return chars <= budget

    whole_request = request(lines)
    if fits(*whole_request):
        requests = [whole_request]
    else:
        reader_context = (
            json.dumps([text for _, text, _ in lines], ensure_ascii=False)
            if output is ReaderReview
            else ""
        )
        requests = []
        start = 0
        while start < len(lines):
            end = start + 1
            current = request(lines[start:end], reader_context)
            if not fits(*current):
                raise StageError(
                    FailureCode.CONTEXT_OVERFLOW,
                    f"Источники для строки {lines[start][0]} превышают бюджет критика",
                )
            while end < len(lines):
                candidate = request(lines[start : end + 1], reader_context)
                if not fits(*candidate):
                    break
                current = candidate
                end += 1
            requests.append(current)
            start = end

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

    problems: list[str] = []
    reader_issues: dict[str, None] = {}
    for messages, schema in requests:
        expected = schema["$defs"]["LineDecision"]["properties"]["line"]["enum"]
        reviewed = ask_structured(
            client,
            critic_profile,
            prompt,
            messages,
            output,
            think=False,
            max_repairs=1,
            seed=seed,
            check=complete,
            schema=schema,
        ).value
        by_line = {decision.line: decision for decision in reviewed.decisions}
        problems.extend(
            key + ": " + by_line[key].reason for key in expected if not by_line[key].supported
        )
        if isinstance(reviewed, ReaderReview):
            reader_issues.update(dict.fromkeys(reviewed.reader_issues))
    checks = [
        CheckResult(
            check=CheckName.CRITIC,
            passed=not problems,
            attempt=attempt,
            details="; ".join(problems) if problems else None,
        )
    ]
    if output is ReaderReview:
        checks.append(
            CheckResult(
                check=CheckName.READER_CONTEXT,
                passed=not reader_issues,
                attempt=attempt,
                details="; ".join(list(reader_issues)[:6]) or None,
            )
        )
    return checks
