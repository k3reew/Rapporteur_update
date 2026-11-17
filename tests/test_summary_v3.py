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
