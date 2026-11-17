"""Содержание сводки: настройки summary.yaml, приоритет выводов и результатов, сокращения."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from rapporteur.contracts import FactsDraft
from rapporteur.contracts.enums import ClaimType
from rapporteur.llm.client import ScriptedClient
from rapporteur.summarize.checks import SummaryChecksConfig, load_checks
from rapporteur.summarize.quotes import QuoteConfig
from rapporteur.summarize.summary import SummaryConfig, write_summary
from rapporteur.summarize.verify import verify_facts

from .fictional_talk import PROFILE, facts_answer, pack, summary_answer


def test_shipped_summary_settings_match_defaults() -> None:
    assert load_checks() == SummaryChecksConfig()


def test_summary_settings_are_read_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "summary.yaml"
    path.write_text("min_sentences: 2\nmax_sentences: 3\npriorities: [result, conclusion]\n")
    checks = load_checks(path)
    assert (checks.min_sentences, checks.max_sentences) == (2, 3)
    assert checks.rank(ClaimType.RESULT) < checks.rank(ClaimType.CONCLUSION)
    assert checks.rank(ClaimType.METHOD) == checks.rank(ClaimType.PROBLEM) == 2
    assert load_checks(tmp_path / "missing.yaml") == SummaryChecksConfig()

    for wrong in (
        "priorities: [result, result]\n",
        "priorities: [listener_remark]\n",
        "min_sentences: 6\nmax_sentences: 5\n",
        "unknown: 1\n",
    ):
        path.write_text(wrong)
        with pytest.raises(ValidationError):
            load_checks(path)


def test_claims_reach_the_model_in_priority_order() -> None:
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "test")
    client = ScriptedClient([summary_answer()])
    write_summary(client, SummaryConfig(profile=PROFILE), facts.title, claims, pack())
    prompt = client.requests[0].messages[1].content
    # c4 — вывод, c3 — результат, c2 — метод, c1 — задача.
    positions = [prompt.index(f"\nc{n} [") for n in (4, 3, 2, 1)]
    assert positions == sorted(positions)
