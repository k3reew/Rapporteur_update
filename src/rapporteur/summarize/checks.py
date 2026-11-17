"""Проверки текста сводки кодом: шаг Г spec §6.7 без критика (критик — Ф4).

Каждая проверка возвращает CheckResult; details перечисляет конкретные нарушения, чтобы их
можно было показать модели при перегенерации и человеку в редакторе.
"""

from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt, model_validator
from razdel import sentenize

from rapporteur import settings
from rapporteur.contracts import Claim, SummaryDraft, SummaryLine, TalkFragmentPack
from rapporteur.contracts.claims import CheckResult
from rapporteur.contracts.enums import CheckName, ClaimStatus, ClaimType
from rapporteur.summarize.quotes import search_form
from rapporteur.summarize.terms import unexplained

# Слова целиком; «*» в конце — любое окончание. Оценочные эпитеты и первое лицо (spec §6.7).
DEFAULT_FORBIDDEN: tuple[str, ...] = (
    "уникальн*",
    "блестящ*",
    "великолепн*",
    "потрясающ*",
    "революционн*",
    "гениальн*",
    "выдающ*",
    "впечатляющ*",
    "замечательн*",
    "прекрасн*",
    "интересн*",
    "несомненн*",
    "безусловн*",
    "мы",
    "нас",
    "нам",
    "нами",
    "наш",
    "наша",
    "наше",
    "наши",
    "нашего",
    "нашей",
    "нашему",
    "нашим",
    "наших",
    "нашими",
    "нашу",
)


# Сокращения, понятные читателю справки без расшифровки. Единицы измерения сюда же:
# «32 ГБ» сокращением для расшифровки не считается.
DEFAULT_KNOWN_ABBREVIATIONS: tuple[str, ...] = (
    "РФ", "США", "ЕС", "СССР", "ООН", "СНГ", "ВВП", "СМИ", "РАН", "ИТ", "IT", "ИИ",
    "КБ", "МБ", "ГБ", "ТБ", "ПБ", "KB", "MB", "GB", "TB",
)  # fmt: skip

# Выводы и результаты важнее общего описания доклада.
DEFAULT_PRIORITIES: tuple[ClaimType, ...] = (
    ClaimType.CONCLUSION,
    ClaimType.RESULT,
    ClaimType.METHOD,
    ClaimType.PROBLEM,
)


class SummaryChecksConfig(BaseModel):
    """Содержание сводки. Значения по умолчанию совпадают с summary.yaml поставки."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_sentences: PositiveInt = 4
    max_sentences: PositiveInt = 5
    forbidden: tuple[str, ...] = DEFAULT_FORBIDDEN
    # Порядок важности типов утверждений; типы, не названные здесь, идут последними.
    priorities: tuple[ClaimType, ...] = DEFAULT_PRIORITIES
    # Сколько утверждений типа текст обязан использовать (не больше, чем их есть).
    # Пусто — без требования; в поставке требование задаёт summary.yaml.
    required: dict[ClaimType, NonNegativeInt] = Field(default_factory=dict)
    # Сокращения в тексте расшифровываются при первом упоминании, кроме общеизвестных.
    explain_abbreviations: bool = False
    known_abbreviations: tuple[str, ...] = DEFAULT_KNOWN_ABBREVIATIONS

    @model_validator(mode="after")
    def _ordered(self) -> "SummaryChecksConfig":
        if self.min_sentences > self.max_sentences:
            raise ValueError("min_sentences больше max_sentences")
        if len(set(self.priorities)) != len(self.priorities):
            raise ValueError("типы в priorities повторяются")
        if ClaimType.LISTENER_REMARK in [*self.priorities, *self.required]:
            raise ValueError("реплики слушателей в текст сводки не входят")
        return self

    def rank(self, claim_type: ClaimType) -> int:
        return (
            self.priorities.index(claim_type)
            if claim_type in self.priorities
            else len(self.priorities)
        )


def load_checks(path: Path | None = None) -> SummaryChecksConfig:
    """Настройки сводки из summary.yaml (settings.SUMMARY_FILE); без файла — значения
    по умолчанию."""
    path = path or settings.SUMMARY_FILE
    if not path.is_file():
        return SummaryChecksConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SummaryChecksConfig.model_validate(data)


def usable_claims(claims: Iterable[Claim]) -> list[Claim]:
    """Утверждения, на которые может опираться текст: прошли сверку и не реплики слушателей."""
    return [
        claim
        for claim in claims
        if claim.status is not ClaimStatus.REJECTED and claim.type is not ClaimType.LISTENER_REMARK
    ]


def sentence_range(config: SummaryChecksConfig, claims: Sequence[Claim]) -> tuple[int, int]:
    """Нижняя граница не выше числа утверждений: иначе модели пришлось бы досочинять."""
    available = len(usable_claims(claims))
    return max(1, min(config.min_sentences, available)), config.max_sentences


CLAIM_NAMES = {
    ClaimType.CONCLUSION: "выводы автора",
    ClaimType.RESULT: "результаты",
    ClaimType.METHOD: "метод",
    ClaimType.PROBLEM: "задача",
}


def required_claims(
    config: SummaryChecksConfig, claims: Sequence[Claim]
) -> list[tuple[ClaimType, int, list[str]]]:
    """Обязательные утверждения по типам в порядке важности: тип, сколько нужно, ключи."""
    usable = usable_claims(claims)
    result = []
    for claim_type in sorted(config.required, key=config.rank):
        keys = [claim.key for claim in usable if claim.type is claim_type]
        need = min(config.required[claim_type], len(keys))
        if need:
            result.append((claim_type, need, keys))
    return result


def required_lines(config: SummaryChecksConfig, claims: Sequence[Claim]) -> list[str]:
    """Требование для промпта: «результаты: не меньше 2 из c3, c5, c6» или все ключи."""
    return [
        f"{CLAIM_NAMES[claim_type]}: {', '.join(keys)}"
        if need == len(keys)
        else f"{CLAIM_NAMES[claim_type]}: не меньше {need} из {', '.join(keys)}"
        for claim_type, need, keys in required_claims(config, claims)
    ]


def coverage_problems(
    draft: SummaryDraft, claims: Sequence[Claim], config: SummaryChecksConfig
) -> list[str]:
    used = {key for line in [*draft.sentences, *draft.conclusions] for key in line.claim_ids}
    problems = []
    for claim_type, need, keys in required_claims(config, claims):
        covered = [key for key in keys if key in used]
        if len(covered) < need:
            missing = [key for key in keys if key not in used]
            problems.append(
                f"{CLAIM_NAMES[claim_type]}: отражено {len(covered)} из нужных {need}, "
                f"используй {', '.join(missing)}"
            )
    return problems


def check_summary(
    draft: SummaryDraft,
    claims: Sequence[Claim],
    pack: TalkFragmentPack,
    config: SummaryChecksConfig,
    attempt: int,
) -> list[CheckResult]:
    checks = {
        CheckName.CLAIM_LINKS: claim_link_problems(draft, claims),
        CheckName.SENTENCE_COUNT: sentence_count_problems(draft, claims, config),
        CheckName.NUMBERS_IN_SOURCES: number_problems(draft, claims, pack),
        CheckName.FORBIDDEN_PHRASES: forbidden_problems(draft, config.forbidden),
    }
    if config.required:
        checks[CheckName.COVERAGE] = coverage_problems(draft, claims, config)
    if config.explain_abbreviations:
        checks[CheckName.ABBREVIATIONS] = abbreviation_problems(draft, config)
    return [
        CheckResult(
            check=name,
            passed=not problems,
            attempt=attempt,
            details="; ".join(problems) if problems else None,
        )
        for name, problems in checks.items()
    ]


def abbreviation_problems(draft: SummaryDraft, config: SummaryChecksConfig) -> list[str]:
    lines = [line.text for line in [*draft.sentences, *draft.conclusions]]
    missing = unexplained(lines, config.known_abbreviations)
    if not missing:
        return []
    return [
        f"сокращения без расшифровки при первом упоминании: {', '.join(missing)}. "
        "Расшифруй по материалам, например «полное название (СУБД)», "
        "или замени описанием без сокращения"
    ]


def claim_link_problems(draft: SummaryDraft, claims: Sequence[Claim]) -> list[str]:
    by_key = {claim.key: claim for claim in claims}
    problems: list[str] = []
    for where, lines in (("предложение", draft.sentences), ("вывод", draft.conclusions)):
        for number, line in enumerate(lines, 1):
            problems.extend(
                f"{where} {number}: {problem}" for problem in _line_problems(line, by_key, where)
            )
    has_conclusions = any(claim.type is ClaimType.CONCLUSION for claim in usable_claims(claims))
    if has_conclusions and not draft.conclusions:
        problems.append("у докладчика есть выводы, а блок выводов пуст")
    return problems


def _line_problems(line: SummaryLine, by_key: dict[str, Claim], where: str) -> Iterable[str]:
    for key in line.claim_ids:
        claim = by_key.get(key)
        if claim is None:
            yield f"неизвестное утверждение {key}"
        elif claim.status is ClaimStatus.REJECTED:
            yield f"утверждение {key} не подтверждено цитатой"
        elif claim.type is ClaimType.LISTENER_REMARK:
            yield f"{key} — реплика слушателя, в текст сводки она не входит"
        elif where == "вывод" and claim.type is not ClaimType.CONCLUSION:
            yield f"{key} не вывод докладчика"


def sentence_count_problems(
    draft: SummaryDraft, claims: Sequence[Claim], config: SummaryChecksConfig
) -> list[str]:
    low, high = sentence_range(config, claims)
    count = sum(len(list(sentenize(line.text))) for line in draft.sentences)
    if low <= count <= high:
        return []
    expected = f"{low}–{high}" if low < high else str(low)
    return [f"в тексте {count} предложений, нужно {expected}"]


def number_problems(
    draft: SummaryDraft, claims: Sequence[Claim], pack: TalkFragmentPack
) -> list[str]:
    """Число есть в источниках привязанных утверждений (форма записи не важна:
    «1 000» и «1000», «94,5» и «94.5» совпадают)."""
    by_key = {claim.key: claim for claim in claims}
    missing: list[str] = []
    for line in [*draft.sentences, *draft.conclusions]:
        if any(
            key not in by_key or by_key[key].status is ClaimStatus.REJECTED
            for key in line.claim_ids
        ):
            continue
        labels = {
            evidence.label
            for key in line.claim_ids
            if key in by_key
            for evidence in by_key[key].evidence
        }
        available = {
            token
            for fragment in pack.fragments
            if fragment.label in labels
            for token in _number_tokens(fragment.text)
        }
        missing.extend(
            token
            for token in _number_tokens(line.text)
            if token not in available and token not in missing
        )
    return (
        [f"числа {', '.join(missing)} нет в привязанных источниках предложения"] if missing else []
    )


def _number_tokens(text: str) -> list[str]:
    return [token for token in search_form(text).text.split(" ") if any(c.isdigit() for c in token)]


def forbidden_problems(draft: SummaryDraft, forbidden: Sequence[str]) -> list[str]:
    found: list[str] = []
    for line in [*draft.sentences, *draft.conclusions]:
        for word in search_form(line.text).text.split(" "):
            for pattern in forbidden:
                if _matches(word, pattern) and word not in found:
                    found.append(word)
    return [f"запрещённые слова: {', '.join(found)}"] if found else []


def _matches(word: str, pattern: str) -> bool:
    pattern = pattern.lower().replace("ё", "е")
    if pattern.endswith("*"):
        return word.startswith(pattern[:-1])
    return word == pattern
