"""New-reader checks remain separate from semantic evidence checks."""

import json
from pathlib import Path

from rapporteur.contracts import FactsDraft, SummaryDraft
from rapporteur.llm.client import ScriptedClient
from rapporteur.summarize.facts import FactsConfig, extract_facts
from rapporteur.summarize.grounding import review_summary
from rapporteur.summarize.quotes import QuoteConfig
from rapporteur.summarize.summary import SummaryConfig, write_summary
from rapporteur.summarize.verify import verify_facts

from .fictional_talk import PROFILE, facts_answer, pack, summary_answer
from .test_grounded_summary import audit_answer

V3 = PROFILE.model_copy(update={"prompt_version": "v3"})


def test_v3_preserves_the_fact_review_stage() -> None:
    client = ScriptedClient([facts_answer(), facts_answer()])
    extract_facts(client, pack(), FactsConfig(profile=V3))
    assert len(client.requests) == 2
    assert "читател" in client.requests[0].messages[0].content.lower()


def test_v3_critic_reports_readability_independently() -> None:
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "v3")
    draft = SummaryDraft.model_validate_json(summary_answer())
    answer = {
        **json.loads(audit_answer()),
        "reader_passed": False,
        "reader_issues": ["Не объяснено назначение рассматриваемого метода."],
    }
    checks = review_summary(
        ScriptedClient([json.dumps(answer)]), V3, draft, claims, pack(), attempt=1, seed=1
    )
    assert [(item.check.value, item.passed) for item in checks] == [
        ("critic", True),
        ("reader_context", False),
    ]


def test_v3_feedback_rewrites_an_opaque_summary() -> None:
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "v3")
    bad = {
        **json.loads(audit_answer()),
        "reader_passed": False,
        "reader_issues": ["Объясни задачу читателю, не видевшему презентацию."],
    }
    good = {**bad, "reader_passed": True, "reader_issues": []}
    client = ScriptedClient([summary_answer(), json.dumps(bad), summary_answer(), json.dumps(good)])
    result = write_summary(client, SummaryConfig(profile=V3), "Кэширование", claims, pack())
    assert result.status.value == "ready"
    assert result.attempts == 2
    assert any("не видевшему" in m.content for m in client.requests[2].messages)
    assert "10" in client.requests[0].messages[0].content


def test_v3_short_source_uses_available_evidence_instead_of_padding() -> None:
    from rapporteur.summarize.checks import sentence_range

    claims = verify_facts(
        FactsDraft.model_validate_json(facts_answer()), pack(), QuoteConfig(), "v3"
    )
    config = SummaryConfig(profile=V3)
    assert config.checks.min_sentences == 6
    assert config.checks.max_sentences == 10
    assert sentence_range(config.checks, claims)[0] <= len(claims)


def test_new_summary_has_two_real_docx_paragraphs(tmp_path: Path) -> None:
    import io

    from docx import Document

    from rapporteur.contracts import SummaryLine
    from rapporteur.contracts.enums import ReportFormat
    from rapporteur.report.assemble import (
        ReportSources,
        build_report_input,
        report_variables,
        select_talks,
        template_context,
    )
    from rapporteur.report.export import render_formats
    from rapporteur.report.template import load_template
    from rapporteur.settings import TEMPLATES_DIR

    from .test_report_build import SOURCES, SUMMARIES

    lines = [
        SummaryLine(text=f"Содержательное предложение {i}.", claim_ids=["c1"]) for i in range(1, 7)
    ]
    current = SUMMARIES[0].model_copy(
        update={"sentences": lines, "prompt_versions": {"summary": "v3"}}
    )
    sources = ReportSources(
        conference=SOURCES.conference,
        sections=SOURCES.sections,
        talks=SOURCES.talks,
        summaries=[current, *SUMMARIES[1:]],
    )
    built = build_report_input(sources, select_talks(sources), "starter-1", True)
    assert "\n\n" in (built.input.talks[0].summary or "")
    formats = render_formats(
        load_template(TEMPLATES_DIR / "starter"),
        template_context(built.input, report_variables(built.input)),
        (ReportFormat.DOCX, ReportFormat.MARKDOWN),
        tmp_path,
    )
    paragraphs = [p.text for p in Document(io.BytesIO(formats[ReportFormat.DOCX])).paragraphs]
    first = " ".join(line.text for line in lines[:3])
    second = " ".join(line.text for line in lines[3:])
    assert first in paragraphs and second in paragraphs
    assert (first + "\n\n" + second) in formats[ReportFormat.MARKDOWN].decode()


def test_short_v3_summary_warns_the_report_operator() -> None:
    from rapporteur.report.assemble import ReportSources, build_report_input, select_talks

    from .test_report_build import SOURCES, SUMMARIES

    current = SUMMARIES[0].model_copy(update={"prompt_versions": {"summary": "v3"}})
    sources = ReportSources(
        conference=SOURCES.conference,
        sections=SOURCES.sections,
        talks=SOURCES.talks,
        summaries=[current, *SUMMARIES[1:]],
    )
    result = build_report_input(sources, select_talks(sources), "starter-1", True)
    assert any("Короткая сводка" in warning for warning in result.warnings)


def test_critic_uses_the_definition_window_when_the_abbreviation_repeats() -> None:
    from rapporteur.summarize.pack import PackSource, build_pack

    from .fictional_talk import CONFERENCE, DECK, SLIDES, TALK, line, slide_draft

    filler = "LRU в таблице. Большая таблица значений. " * 2600
    definition = "LRU (least recently used). Определение не подтверждает выигрыш."
    source = build_pack(
        CONFERENCE,
        TALK,
        [
            PackSource(
                DECK,
                [
                    *[slide_draft(n, t) for n, t in enumerate(SLIDES, 1)],
                    slide_draft(6, filler + definition + filler),
                ],
            )
        ],
    )
    claims = verify_facts(
        FactsDraft.model_validate_json(facts_answer()), source, QuoteConfig(), "v3"
    )
    items = [line("Автор сравнил альтернативы, включая LRU (least recently used).", "c2")] * 2
    draft = SummaryDraft.model_validate({"sentences": items, "conclusions": []})
    answer = {
        "decisions": [{"line": key, "supported": True, "reason": "Есть"} for key in ("s1", "s2")],
        "reader_passed": True,
        "reader_issues": [],
    }
    client = ScriptedClient([json.dumps(answer)])
    assert review_summary(
        client, V3, draft, claims, source, attempt=1, seed=1, glossary={"LRU": "S6"}
    )[0].passed
    sources = json.loads(client.requests[0].messages[1].content)
    assert all(
        any(definition in item["text"] and len(item["text"]) < 6000 for item in row["sources"])
        for row in sources
    )
    assert len(source.fragments[-1].text) > 200_000


def test_critic_paginates_requests_and_preserves_decisions_and_reader_issues() -> None:
    from rapporteur.llm.client import ChatRequest, ChatResponse
    from rapporteur.summarize.pack import PackSource, build_pack

    from .fictional_talk import CONFERENCE, DECK, SLIDES, TALK, line, slide_draft

    slides = list(SLIDES)
    slides[2] += " Дополнительные данные о методе." * 165
    source = build_pack(
        CONFERENCE, TALK, [PackSource(DECK, [slide_draft(n, t) for n, t in enumerate(slides, 1)])]
    )
    claims = verify_facts(
        FactsDraft.model_validate_json(facts_answer()), source, QuoteConfig(), "v3"
    )
    draft = SummaryDraft.model_validate(
        {
            "sentences": [line(f"Описание метода {n}.", "c2") for n in range(1, 11)],
            "conclusions": [line("Вывод из доклада.", "c2")],
        }
    )

    class BudgetClient:
        def __init__(self) -> None:
            self.pages: list[list[str]] = []

        def chat(self, request: ChatRequest) -> ChatResponse:
            assert request.schema
            chars = sum(len(msg.content) for msg in request.messages) + len(
                json.dumps(request.schema)
            )
            assert chars // 4 + request.max_tokens <= request.context_tokens
            ids = request.schema["$defs"]["LineDecision"]["properties"]["line"]["enum"]
            self.pages.append(ids)
            assert "Описание метода 1." in request.messages[1].content
            assert "Вывод из доклада." in request.messages[1].content
            issues = ["Данные требуют пояснения."] if "c1" not in ids else ["Уточни результат."]
            if "s1" in ids:
                issues = []
            result = {
                "decisions": [
                    {"line": key, "supported": key not in {"s1", "c1"}, "reason": "Решение"}
                    for key in ids
                ],
                "reader_passed": not issues,
                "reader_issues": issues,
            }
            return ChatResponse(json.dumps(result), "", "stop", 100, 100, 0)

    client = BudgetClient()
    critic, reader = review_summary(client, V3, draft, claims, source, attempt=2, seed=1)
    assert len(client.pages) > 1
    assert [key for page in client.pages for key in page] == [
        *(f"s{n}" for n in range(1, 11)),
        "c1",
    ]
    assert critic.details == "s1: Решение; c1: Решение"
    assert not reader.passed and reader.attempt == 2
    assert reader.details and "Уточни результат." in reader.details


def test_critic_reserves_only_the_output_needed_for_a_small_draft() -> None:
    from .fictional_talk import line

    source = pack()
    claims = verify_facts(
        FactsDraft.model_validate_json(facts_answer()), source, QuoteConfig(), "v3"
    )
    draft = SummaryDraft.model_validate(
        {"sentences": [line("Автор сравнил две политики.", "c2")], "conclusions": []}
    )
    profile = V3.model_copy(
        update={"generation": V3.generation.model_copy(update={"max_tokens": 8000})}
    )
    answer = {
        "decisions": [{"line": "s1", "supported": True, "reason": "Есть"}],
        "reader_passed": True,
        "reader_issues": [],
    }
    client = ScriptedClient([json.dumps(answer)])
    assert review_summary(client, profile, draft, claims, source, attempt=1, seed=1)[0].passed
    assert 240 <= client.requests[0].max_tokens < 8000
    assert profile.generation.max_tokens == 8000
