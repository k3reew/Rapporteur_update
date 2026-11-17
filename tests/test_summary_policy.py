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

from .fictional_talk import PROFILE, SENTENCES, facts_answer, line, pack, summary_answer

PROFILE_V3 = PROFILE.model_copy(update={"prompt_version": "v3"})
REQUIRED = SummaryChecksConfig(required={ClaimType.CONCLUSION: 3, ClaimType.RESULT: 3})


def test_shipped_summary_settings_require_conclusions_and_results() -> None:
    checks = load_checks()
    assert checks.required == {ClaimType.CONCLUSION: 3, ClaimType.RESULT: 3}
    # Остальное совпадает со значениями по умолчанию в коде.
    assert checks.model_copy(update={"required": {}}) == SummaryChecksConfig()


def test_every_profile_has_all_prompts_of_its_edition() -> None:
    import yaml

    from rapporteur.llm.prompts import edition, load_prompt
    from rapporteur.settings import PROFILES_FILE

    profiles = yaml.safe_load(PROFILES_FILE.read_text(encoding="utf-8"))["profiles"]
    for profile in profiles:
        assert edition(profile["prompt_version"]) == 3
        for step in ("facts", "facts-review", "summary", "critic"):
            load_prompt(step, profile["prompt_version"])


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


def critic_answer() -> str:
    import json

    return json.dumps(
        {
            "decisions": [
                {"line": key, "supported": True, "reason": "Поддерживается источником"}
                for key in ["s1", "s2", "s3", "s4", "c1"]
            ]
        },
        ensure_ascii=False,
    )


def test_summary_must_use_results_and_conclusions() -> None:
    from rapporteur.contracts.enums import CheckName, SummaryStatus

    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "test")
    without_result = [
        SENTENCES[0],
        SENTENCES[1],
        line("Сравнивались две политики кэширования.", "c2"),
        line("Работа направлена на снижение задержки чтения.", "c1"),
    ]
    client = ScriptedClient([summary_answer(without_result), summary_answer(), critic_answer()])
    config = SummaryConfig(profile=PROFILE_V3, checks=REQUIRED)
    result = write_summary(client, config, facts.title, claims, pack())

    assert result.status is SummaryStatus.READY and result.attempts == 2
    system = client.requests[0].messages[0].content
    assert "Порядок важности сведений: выводы автора, результаты, метод, задача" in system
    assert "- выводы автора: c4" in system and "- результаты: c3" in system
    assert (
        "результаты: отражено 0 из нужных 1, используй c3"
        in client.requests[1].messages[-1].content
    )
    coverage = [(c.attempt, c.passed) for c in result.checks if c.check is CheckName.COVERAGE]
    assert coverage == [(1, False), (2, True)]


def test_coverage_asks_for_some_of_many_results() -> None:
    from rapporteur.contracts import SummaryDraft
    from rapporteur.summarize.checks import coverage_problems, required_lines

    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "test")
    results = [claims[2].model_copy(update={"key": key}) for key in ("c3", "c5", "c6")]
    many = [*claims[:2], *results, claims[3]]
    config = SummaryChecksConfig(required={ClaimType.RESULT: 2})
    assert required_lines(config, many) == ["результаты: не меньше 2 из c3, c5, c6"]
    draft = SummaryDraft.model_validate_json(summary_answer())
    assert coverage_problems(draft, many, config) == [
        "результаты: отражено 1 из нужных 2, используй c5, c6"
    ]
    assert coverage_problems(draft, many, SummaryChecksConfig()) == []
