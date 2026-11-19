"""Bounded preparation tasks recover from truncated model output."""

import json

import pytest

from rapporteur import preparation
from rapporteur.errors import FailureCode, StageError
from rapporteur.llm.client import ChatResponse, ScriptedClient
from rapporteur.llm.tasks import json_task
from rapporteur.preparation import MaterialMatch, Visual

from .fictional_talk import PROFILE


def cut() -> ChatResponse:
    return ChatResponse('{"description":"unfinished', "", "length", 120, 2000, 0.1)


def test_truncated_task_retries_without_the_broken_answer() -> None:
    client = ScriptedClient([cut(), '{"description":"Два узла соединены каналом."}'])
    result = json_task(client, PROFILE, "Опиши схему.", "Слайд 7", Visual, ("image",))
    assert result.description == "Два узла соединены каналом."
    assert len(client.requests) == 2
    assert not any(m.role == "assistant" for m in client.requests[1].messages)
    assert client.requests[1].messages[1].images == ("image",)
    assert "крат" in client.requests[1].messages[-1].content.lower()


def test_long_transcript_windows_keep_all_absolute_indices() -> None:
    texts = [f"segment-{i}: " + "слово " * 70 for i in range(13)]
    texts[5] += " ".join(f"длинный-{i}" for i in range(300))
    windows = list(preparation.transcript_windows(texts, 1000))
    assert len(windows) > 1
    assert {i for window in windows for i, _ in window} == set(range(13))
    assert all(sum(len(text) + 20 for _, text in window) <= 1000 for window in windows)
    for index, original in enumerate(texts):
        parts = list(dict.fromkeys(text for window in windows for i, text in window if i == index))
        assert "".join(parts) == original


def test_a_second_truncation_reports_the_task_and_token_limits() -> None:
    client = ScriptedClient([cut(), cut()])
    with pytest.raises(StageError) as failure:
        json_task(client, PROFILE, "Опиши схему.", "Слайд 7", Visual, task="vision:slide-7")
    assert failure.value.code is FailureCode.LLM_TRUNCATED
    assert "vision:slide-7" in failure.value.message
    assert "2000" in failure.value.message
    assert "16000" in failure.value.message
    assert len(client.requests) == 2


def test_structural_repair_and_compact_retry_share_three_attempts() -> None:
    client = ScriptedClient(["not JSON", cut(), '{"description":"Сеть из двух узлов."}'])
    result = json_task(client, PROFILE, "Опиши схему.", "Слайд 7", Visual)
    assert result.description == "Сеть из двух узлов."
    assert len(client.requests) == 3
    assert not any(m.role == "assistant" for m in client.requests[-1].messages)


def test_visual_description_is_bounded_in_the_generation_schema() -> None:
    client = ScriptedClient(['{"description":""}'])
    json_task(client, PROFILE, "Опиши схему.", "Слайд 7", Visual)
    assert client.requests[0].schema is not None
    assert client.requests[0].schema["properties"]["description"]["maxLength"] == 1500


def test_task_uses_allowed_identifiers_and_repairs_foreign_values() -> None:
    reply = {"title": "Тема", "speakers": [], "talk_index": 9, "confidence": 0.9, "reason": "Связь"}
    fixed = {**reply, "talk_index": 1}
    schema = MaterialMatch.model_json_schema()
    schema["properties"]["talk_index"] = {"enum": [None, 1]}
    client = ScriptedClient([json.dumps(reply), json.dumps(fixed)])
    result = json_task(
        client,
        PROFILE,
        "Сопоставь.",
        "Кандидат 1",
        MaterialMatch,
        schema=schema,
        check=lambda value: [] if value.talk_index in (None, 1) else ["Неизвестный доклад"],
    )
    assert result.talk_index == 1
    assert client.requests[0].schema == schema
    assert len(client.requests) == 2


def test_large_candidate_registry_is_chunked_with_absolute_indices() -> None:
    from rapporteur.preparation import candidate_windows

    candidates = [{"index": i, "title": "Длинная тема " * 80, "speakers": []} for i in range(100)]
    batches = list(candidate_windows(candidates, 4000))
    assert len(batches) > 1
    assert [c["index"] for batch in batches for c in batch] == list(range(100))
    assert all(len(json.dumps(batch, ensure_ascii=False)) <= 4000 for batch in batches)


def test_invalid_answers_are_explained_to_the_model_and_in_the_error() -> None:
    """Модель получает поле и причину без служебного текста Pydantic, каждый повтор немного
    разнообразнее, итоговая ошибка называет, что не прошло в каждой попытке."""
    wrong = '{"title": null, "speakers": [], "talk_index": 7, "confidence": 2, "reason": "x"}'
    client = ScriptedClient([wrong, wrong, wrong])
    with pytest.raises(StageError) as error:
        json_task(client, PROFILE, "Сопоставь.", "{}", MaterialMatch, task="Тема и авторы a.pptx")
    assert error.value.code is FailureCode.LLM_INVALID_OUTPUT
    assert "Тема и авторы a.pptx: модель трижды вернула неверную структуру" in error.value.message
    assert "попытка 1: confidence:" in error.value.message
    assert "попытка 3: confidence:" in error.value.message
    repair = client.requests[1].messages[-1].content
    assert "- confidence:" in repair and "pydantic.dev" not in repair
    assert [request.temperature for request in client.requests] == [0.1, 0.35, 0.6]


def test_structured_repair_raises_temperature_only_on_retries() -> None:
    from rapporteur.contracts import FactsDraft
    from rapporteur.summarize.facts import FactsConfig, extract_facts

    from .fictional_talk import facts_answer, pack

    client = ScriptedClient(["не JSON", facts_answer()])
    extract_facts(client, pack(), FactsConfig(profile=PROFILE))
    base = PROFILE.generation.temperature
    assert [request.temperature for request in client.requests] == [base, base + 0.25]
    assert FactsDraft.model_validate_json(facts_answer())
