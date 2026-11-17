"""Регрессии локального пилота. Модель заменена только на границе генерации."""

import json

import pytest

from rapporteur.contracts import FactsDraft, SummaryDraft
from rapporteur.contracts.enums import CheckName, SummaryStatus
from rapporteur.errors import FailureCode, StageError
from rapporteur.llm.client import ScriptedClient
from rapporteur.llm.prompts import load_prompt
from rapporteur.llm.structured import ask_structured
from rapporteur.summarize.checks import SummaryChecksConfig, check_summary
from rapporteur.summarize.facts import FactsConfig, extract_facts
from rapporteur.summarize.pack import PackSource, build_pack
from rapporteur.summarize.quotes import QuoteConfig
from rapporteur.summarize.summary import SummaryConfig, write_summary
from rapporteur.summarize.verify import verify_facts

from .fictional_talk import (
    CONFERENCE,
    DECK,
    PROFILE,
    SENTENCES,
    TALK,
    facts_answer,
    line,
    pack,
    slide_draft,
    summary_answer,
)

PROFILE_V2 = PROFILE.model_copy(update={"prompt_version": "v2"})


def audit_answer(bad: str | None = None) -> str:
    return json.dumps(
        {
            "decisions": [
                {
                    "line": key,
                    "supported": key != bad,
                    "reason": "Потеряно отрицание" if key == bad else "Поддерживается источником",
                }
                for key in ["s1", "s2", "s3", "s4", "c1"]
            ]
        },
        ensure_ascii=False,
    )


def test_fact_schema_limits_evidence_to_actual_source_labels() -> None:
    client = ScriptedClient([facts_answer()])
    extract_facts(client, pack(), FactsConfig(profile=PROFILE))
    assert client.requests[0].schema is not None
    labels = client.requests[0].schema["$defs"]["EvidenceDraft"]["properties"]["label"]
    assert labels["enum"] == ["S1", "S2", "S3", "S4", "S5", "N1"]


def test_v2_fact_review_keeps_ideal_outcome_as_a_proposal() -> None:
    source = build_pack(
        CONFERENCE,
        TALK,
        [
            PackSource(
                DECK,
                [
                    slide_draft(
                        1,
                        "Идеальный итог\nПроект достиг критической массы\n"
                        "Есть постоянные участники",
                    )
                ],
            )
        ],
    )
    wrong = facts_answer(
        problem=None,
        methods=[],
        results=[
            {
                "text": "Проект достиг критической массы",
                "evidence": [{"label": "S1", "quote": "Проект достиг критической массы"}],
            }
        ],
        conclusions=[],
        conclusions_absent_reason="нет",
        listener_remarks=[],
    )
    corrected = json.loads(wrong)
    corrected["results"][0]["text"] = (
        "Автор описывает достижение критической массы как идеальный итог"
    )
    client = ScriptedClient([wrong, json.dumps(corrected, ensure_ascii=False)])
    result = extract_facts(client, source, FactsConfig(profile=PROFILE_V2))
    assert result.facts.results[0].text == corrected["results"][0]["text"]
    assert result.attempts == 2
    assert "Идеальный итог" in client.requests[1].messages[-1].content


def test_v2_summary_critic_repairs_semantic_drift() -> None:
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "test")
    wrong = [*SENTENCES[:2], line("LFU увеличила задержку на 23 %.", "c3"), SENTENCES[3]]
    client = ScriptedClient(
        [
            summary_answer(wrong),
            audit_answer("s3"),
            summary_answer(),
            audit_answer(),
        ]
    )
    result = write_summary(client, SummaryConfig(profile=PROFILE_V2), facts.title, claims, pack())
    assert result.status is SummaryStatus.READY
    assert result.sentences[2].text == "Показано, что политика LFU снизила задержку на 23 %."
    assert result.attempts == 2
    assert [(c.attempt, c.passed) for c in result.checks if c.check is CheckName.CRITIC] == [
        (1, False),
        (2, True),
    ]
    assert "[S4] Результаты: при нагрузке 1000" in client.requests[0].messages[1].content


def test_critic_must_review_every_line_before_acceptance() -> None:
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "test")
    incomplete = '{"decisions": [{"line":"s1","supported":true,"reason":"Да"}]}'
    client = ScriptedClient([summary_answer(), incomplete, audit_answer()])
    result = write_summary(client, SummaryConfig(profile=PROFILE_V2), facts.title, claims, pack())
    assert result.status is SummaryStatus.READY
    assert len(client.requests) == 3
    assert any(c.check is CheckName.CRITIC and c.passed for c in result.checks)


def test_number_in_an_unlinked_source_does_not_support_a_sentence() -> None:
    source = pack()
    extra = build_pack(
        CONFERENCE,
        TALK,
        [PackSource(DECK, [slide_draft(6, "Другое исследование показало рост на 25 %.")])],
    ).fragments[0]
    source = source.model_copy(update={"fragments": [*source.fragments, extra]})
    claims = verify_facts(
        FactsDraft.model_validate_json(facts_answer()), source, QuoteConfig(), "t"
    )
    wrong = [*SENTENCES[:2], line("LFU снизила задержку на 25 %.", "c3"), SENTENCES[3]]
    draft = SummaryDraft.model_validate_json(summary_answer(wrong))
    checks = check_summary(draft, claims, source, SummaryChecksConfig(), 1)
    assert any(c.check is CheckName.NUMBERS_IN_SOURCES and not c.passed for c in checks)


def test_repair_history_is_checked_before_a_second_request() -> None:
    client = ScriptedClient(["x" * 12000, facts_answer()])
    profile = PROFILE.model_copy(
        update={"generation": PROFILE.generation.model_copy(update={"context_tokens": 6000})}
    )
    prompt = load_prompt("facts", "v1")
    from rapporteur.summarize.facts import facts_messages

    with pytest.raises(StageError) as error:
        ask_structured(
            client,
            profile,
            prompt,
            facts_messages(prompt, pack()),
            FactsDraft,
            think=True,
            max_repairs=2,
            seed=1,
        )
    assert error.value.code is FailureCode.CONTEXT_OVERFLOW
    assert len(client.requests) == 1


def test_critic_reads_adjacent_asr_context_from_the_same_recording() -> None:
    from rapporteur.contracts import FragmentDraft
    from rapporteur.contracts.enums import FragmentKind, TextOrigin
    from rapporteur.contracts.fragments import TimeLocator
    from rapporteur.summarize.grounding import review_summary

    fragments = [
        FragmentDraft(
            kind=FragmentKind.TRANSCRIPT,
            source_sha256="a" * 64,
            locator=TimeLocator(kind="time", start_ms=start, end_ms=start + 10000),
            raw_text=text,
            normalized_text=text,
            origin=TextOrigin.ASR,
            engine_confidence=None,
        )
        for start, text in [
            (0, "Идеальный итог, которого мы хотим достичь."),
            (10000, "Проект достиг критической массы."),
        ]
    ]
    source = build_pack(CONFERENCE, TALK, [], transcripts=[PackSource(DECK, fragments)])
    data = FactsDraft.model_validate_json(
        facts_answer(
            problem=None,
            methods=[],
            results=[
                {
                    "text": "Проект достиг критической массы.",
                    "evidence": [
                        {"label": "T1.00:00:10", "quote": "Проект достиг критической массы"}
                    ],
                }
            ],
            conclusions=[],
            conclusions_absent_reason="нет",
            listener_remarks=[],
        )
    )
    claims = verify_facts(data, source, QuoteConfig(), "t")
    draft = SummaryDraft.model_validate(
        {"sentences": [line(claims[0].text, "c1")], "conclusions": []}
    )
    client = ScriptedClient(['{"decisions":[{"line":"s1","supported":false,"reason":"Это цель"}]}'])
    check = review_summary(client, PROFILE_V2, draft, claims, source, attempt=1, seed=1)
    assert not check[0].passed
    assert "Идеальный итог, которого мы хотим достичь" in client.requests[0].messages[1].content


def test_critic_uses_bounded_windows_for_an_oversized_source() -> None:
    from rapporteur.summarize.grounding import review_summary

    text = "Результаты\n" + "Дополнительные сведения. " * 5000 + "\nLFU снизила задержку на 23 %."
    source = build_pack(CONFERENCE, TALK, [PackSource(DECK, [slide_draft(1, text)])])
    data = FactsDraft.model_validate_json(
        facts_answer(
            problem=None,
            methods=[],
            results=[
                {
                    "text": "LFU снизила задержку на 23 %.",
                    "evidence": [{"label": "S1", "quote": "LFU снизила задержку на 23 %"}],
                }
            ],
            conclusions=[],
            conclusions_absent_reason="нет",
            listener_remarks=[],
        )
    )
    claims = verify_facts(data, source, QuoteConfig(), "t")
    draft = SummaryDraft.model_validate(
        {"sentences": [line(claims[0].text, "c1")], "conclusions": []}
    )
    client = ScriptedClient(['{"decisions":[{"line":"s1","supported":true,"reason":"Да"}]}'])
    check = review_summary(client, PROFILE_V2, draft, claims, source, attempt=1, seed=1)
    assert check[0].passed
    content = client.requests[0].messages[1].content
    assert "Результаты" in content and "LFU снизила задержку" in content
    assert len(content) < 6000


def test_repeated_fact_arrays_are_bounded_in_schema_and_validation() -> None:
    oversized = json.loads(facts_answer())
    oversized["results"] = oversized["results"] * 6
    client = ScriptedClient(
        [json.dumps(oversized, ensure_ascii=False), facts_answer(), facts_answer()]
    )
    result = extract_facts(client, pack(), FactsConfig(profile=PROFILE_V2))
    assert result.attempts == 3
    assert len(result.facts.results) == 1
    assert client.requests[0].schema is not None
    assert all(
        branch["properties"]["results"]["maxItems"] == 5
        for branch in client.requests[0].schema["oneOf"]
    )


def test_critic_explanation_is_bounded_and_invalid_answers_are_repaired() -> None:
    from rapporteur.summarize.grounding import review_summary

    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "t")
    draft = SummaryDraft.model_validate_json(summary_answer())
    oversized = json.loads(audit_answer())
    oversized["decisions"][0]["reason"] = "Повтор объяснения. " * 100
    client = ScriptedClient([json.dumps(oversized, ensure_ascii=False), audit_answer()])
    result = review_summary(client, PROFILE_V2, draft, claims, pack(), attempt=1, seed=1)
    assert result[0].passed
    assert len(client.requests) == 2
    schema = client.requests[0].schema
    assert schema is not None
    assert schema["properties"]["decisions"]["maxItems"] == 5
    assert schema["$defs"]["LineDecision"]["properties"]["reason"]["maxLength"] == 240


def test_fact_schema_encodes_the_conclusions_reason_dependency() -> None:
    client = ScriptedClient([facts_answer()])
    extract_facts(client, pack(), FactsConfig(profile=PROFILE))
    schema = client.requests[0].schema
    assert schema is not None
    absent, present = schema["oneOf"]
    assert absent["properties"]["conclusions"]["maxItems"] == 0
    assert absent["properties"]["conclusions_absent_reason"]["type"] == "string"
    assert present["properties"]["conclusions"]["minItems"] == 1
    assert present["properties"]["conclusions_absent_reason"]["type"] == "null"


def test_reduction_explicitly_combines_all_parts_as_one_json_array() -> None:
    import re
    from uuid import uuid4

    from rapporteur.contracts.enums import TextOrigin
    from rapporteur.ingest.documents import fragment

    drafts = [
        fragment("a" * 64, i, "Материал страницы. " * 400, TextOrigin.EXTRACTED)
        for i in range(1, 5)
    ]
    source = build_pack(uuid4(), uuid4(), [PackSource(uuid4(), drafts)])
    profile = PROFILE_V2.model_copy(
        update={"generation": PROFILE_V2.generation.model_copy(update={"max_tokens": 2000})}
    )
    parts = [re.sub(r'"S\d+"', f'"D{i}"', facts_answer(listener_remarks=[])) for i in (1, 3)]
    client = ScriptedClient([parts[0], parts[0], parts[1], parts[1], parts[0]])
    extract_facts(client, source, FactsConfig(profile=profile))
    content = client.requests[-1].messages[-1].content
    assert "Объедини факты всех частей" in content
    array = (
        content.split("Части фактов в JSON-массиве.\n", 1)[1]
        .split("\n\nИсходные фрагменты", 1)[0]
        .strip()
    )
    assert [part["results"][0]["evidence"][0]["label"] for part in json.loads(array)] == [
        "D1",
        "D3",
    ]


def test_fallback_prefers_complete_linked_draft_over_broken_later_attempt() -> None:
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "fallback")
    broken = [line(SENTENCES[0]["text"], "c999"), *SENTENCES[1:]]
    client = ScriptedClient(
        [summary_answer(), audit_answer("s1"), summary_answer(broken), summary_answer(broken)]
    )
    result = write_summary(client, SummaryConfig(profile=PROFILE_V2), facts.title, claims, pack())
    assert result.status is SummaryStatus.NEEDS_ATTENTION
    assert len(result.sentences) == 4
    assert [item.text for item in result.sentences] == [item["text"] for item in SENTENCES]
