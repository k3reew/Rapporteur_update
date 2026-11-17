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

PROFILE_V4 = PROFILE.model_copy(update={"prompt_version": "v4"})
REQUIRED = SummaryChecksConfig(required={ClaimType.CONCLUSION: 3, ClaimType.RESULT: 3})


def test_shipped_summary_settings_require_conclusions_and_results() -> None:
    from rapporteur.settings import ROOT

    checks = load_checks(ROOT / "summary.yaml")
    assert checks.required == {ClaimType.CONCLUSION: 3, ClaimType.RESULT: 3}
    assert checks.explain_abbreviations
    # Длина как у редакции v3 выпуска 0.3.0, остальное — значения по умолчанию в коде.
    assert (checks.min_sentences, checks.max_sentences) == (6, 10)
    plain = checks.model_copy(
        update={
            "required": {},
            "explain_abbreviations": False,
            "min_sentences": 4,
            "max_sentences": 5,
        }
    )
    assert plain == SummaryChecksConfig()


def test_every_profile_has_all_prompts_of_its_edition() -> None:
    import yaml

    from rapporteur.llm.prompts import edition, load_prompt
    from rapporteur.settings import PROFILES_FILE

    profiles = yaml.safe_load(PROFILES_FILE.read_text(encoding="utf-8"))["profiles"]
    for profile in profiles:
        assert edition(profile["prompt_version"]) == 4
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
            ],
            # Критик с редакции v3 также оценивает понятность новому читателю.
            "reader_passed": True,
            "reader_issues": [],
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
    config = SummaryConfig(profile=PROFILE_V4, checks=REQUIRED)
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


def test_abbreviations_and_their_definitions_are_found() -> None:
    from rapporteur.summarize.pack import PackSource, build_pack
    from rapporteur.summarize.terms import abbreviations, find_definitions, unexplained

    from .fictional_talk import CONFERENCE, DECK, TALK, slide_draft

    assert abbreviations("СУБД и LFU-кэш в XXI веке, COVID-19, 32 ГБ; снова СУБД") == [
        "СУБД",
        "LFU",
        "COVID-19",
        "ГБ",
    ]
    source = build_pack(
        CONFERENCE,
        TALK,
        [
            PackSource(
                DECK,
                [
                    slide_draft(1, "Используется система управления базами данных (СУБД)."),
                    slide_draft(2, "Политика LFU (вытеснение редко используемых записей)."),
                    slide_draft(3, "ЦОД — центр обработки данных, где стоит стенд"),
                    slide_draft(4, "Новая политика Least Recently Used (LRU) проще"),
                ],
            )
        ],
    )
    found = find_definitions(source, ["СУБД", "LFU", "ЦОД", "LRU", "API"])
    assert {key: (item.definition, item.label) for key, item in found.items()} == {
        "СУБД": ("система управления базами данных", "S1"),
        "LFU": ("вытеснение редко используемых записей", "S2"),
        "ЦОД": ("центр обработки данных, где стоит стенд", "S3"),
        "LRU": ("Least Recently Used", "S4"),
    }
    lines = [
        "Используется система управления базами данных (СУБД), СУБД хранит данные.",
        "LFU (вытеснение редко используемых записей) снизила задержку.",
        "ЦОД — центр обработки данных.",
        "Объём 32 ГБ, а LRU быстрее; в РФ.",
    ]
    assert unexplained(lines, ["ГБ", "РФ"]) == ["LRU"]


def test_summary_without_definitions_avoids_abbreviations() -> None:
    from rapporteur.contracts.enums import CheckName, SummaryStatus

    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, pack(), QuoteConfig(), "test")
    plain = [
        SENTENCES[0],
        line("Автор сравнил две политики вытеснения записей на стенде из 4 узлов.", "c2"),
        line("Показано, что одна из политик снизила задержку на 23 %.", "c3"),
        SENTENCES[3],
    ]
    conclusion = [line("Одна из политик эффективнее при неравномерной нагрузке.", "c4")]
    client = ScriptedClient([summary_answer(), summary_answer(plain, conclusion), critic_answer()])
    checks = SummaryChecksConfig(explain_abbreviations=True)
    result = write_summary(
        client, SummaryConfig(profile=PROFILE_V4, checks=checks), facts.title, claims, pack()
    )
    assert result.status is SummaryStatus.READY and result.attempts == 2
    # Порядок — по утверждениям в порядке важности: вывод c4 упоминает LFU первым.
    assert "Расшифровки нет в материалах: LFU, LRU." in client.requests[0].messages[1].content
    assert "Не расшифровывай общеизвестные сокращения: РФ" in client.requests[0].messages[0].content
    repair = client.requests[1].messages[-1].content
    assert "сокращения без расшифровки при первом упоминании: LRU, LFU" in repair
    states = [(c.attempt, c.passed) for c in result.checks if c.check is CheckName.ABBREVIATIONS]
    assert states == [(1, False), (2, True)]


def test_critic_sees_the_fragment_with_the_definition() -> None:
    import json

    from rapporteur.contracts.enums import SummaryStatus
    from rapporteur.summarize.pack import PackSource, build_pack

    from .fictional_talk import CONFERENCE, DECK, SLIDES, TALK, slide_draft

    slides = [*SLIDES, "Термины: LRU (вытеснение давно не использованных записей)."]
    source = build_pack(
        CONFERENCE, TALK, [PackSource(DECK, [slide_draft(n, t) for n, t in enumerate(slides, 1)])]
    )
    facts = FactsDraft.model_validate_json(facts_answer())
    claims = verify_facts(facts, source, QuoteConfig(), "test")
    explained = [
        SENTENCES[0],
        line(
            "Автор сравнил политику вытеснения давно не использованных записей (LRU) "
            "и одну из альтернатив на стенде из 4 узлов.",
            "c2",
        ),
        line("Показано, что альтернативная политика снизила задержку на 23 %.", "c3"),
        SENTENCES[3],
    ]
    conclusion = [line("Альтернатива эффективнее LRU при неравномерной нагрузке.", "c4")]
    client = ScriptedClient([summary_answer(explained, conclusion), critic_answer()])
    checks = SummaryChecksConfig(explain_abbreviations=True)
    result = write_summary(
        client, SummaryConfig(profile=PROFILE_V4, checks=checks), facts.title, claims, source
    )
    assert result.status is SummaryStatus.READY
    assert "LRU — вытеснение давно не использованных записей [S6]" in (
        client.requests[0].messages[1].content
    )
    reviewed = json.loads(client.requests[1].messages[1].content)
    s2 = next(item for item in reviewed if item["line"] == "s2")
    assert [source["label"] for source in s2["sources"]] == ["S3", "S6"]
