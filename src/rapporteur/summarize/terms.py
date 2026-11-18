"""Сокращения в сводке: расшифровка при первом упоминании или текст без сокращения.

Читатель справки не видел доклада, поэтому «LFU» или «СУБД» без расшифровки ему непонятны.
Код ищет расшифровки в материалах доклада в трёх видах: «полное название (СУБД)»,
«СУБД (полное название)» и «СУБД — определение». Модель получает найденные расшифровки
вместе с утверждениями, а сокращения без расшифровки в материалах описывает словами или
опускает. Проверка кодом требует расшифровку при первом упоминании в тексте сводки; общеизвестные
сокращения перечислены в summary.yaml.
"""

import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from rapporteur.contracts import TalkFragmentPack

# Два заглавных и больше, цифры и части через дефис: СУБД, LFU, COVID-19, ГОСТ-Р.
# «LFU-кэш» даёт LFU. Римские числа (XXI век) сокращениями не считаются.
_ABBREVIATION = re.compile(
    r"(?<![\w-])[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]*[A-ZА-ЯЁ][A-ZА-ЯЁ0-9]*(?:-[A-ZА-ЯЁ0-9]+)*(?!\w)"
)
_ROMAN = re.compile(r"[IVXLCDM]+")
_WORD = re.compile(r"[^\W\d_][\w-]*")
_LOWER = re.compile(r"[a-zа-яё]")
_MAX_DEFINITION = 120


@dataclass(frozen=True)
class Definition:
    abbreviation: str
    definition: str
    label: str


def abbreviations(text: str) -> list[str]:
    """Сокращения текста в порядке первого появления."""
    found = (match.group(0) for match in _ABBREVIATION.finditer(text))
    return list(dict.fromkeys(item for item in found if not _ROMAN.fullmatch(item)))


def find_definitions(pack: TalkFragmentPack, wanted: Iterable[str]) -> dict[str, Definition]:
    """Первая расшифровка каждого сокращения в порядке фрагментов пакета."""
    result: dict[str, Definition] = {}
    for abbreviation in wanted:
        for fragment in pack.fragments:
            match = definition_at(fragment.text, abbreviation)
            if match:
                result[abbreviation] = Definition(abbreviation, match[0], fragment.label)
                break
    return result


def unexplained(lines: Sequence[str], known: Collection[str]) -> list[str]:
    """Сокращения без расшифровки при первом упоминании, кроме общеизвестных."""
    text = " ".join(lines)
    missing = []
    for abbreviation in abbreviations(text):
        if abbreviation in known:
            continue
        match = next(
            match for match in _ABBREVIATION.finditer(text) if match.group(0) == abbreviation
        )
        if not _explained(text, match.start(), match.end()):
            missing.append(abbreviation)
    return missing


def _explained(text: str, start: int, end: int) -> bool:
    before, after = text[:start].rstrip(), text[end:]
    # «система управления базами данных (СУБД)» или «(СУБД, ...)»
    if before.endswith("(") and _WORD.search(before[:-1]) and after.lstrip()[:1] in ("),;"):
        return True
    # «СУБД (система управления базами данных)»
    parenthetical = re.match(r"\s*\(([^)]{3,})\)", after)
    if parenthetical and _LOWER.search(parenthetical.group(1)):
        return True
    # «LFU — политика вытеснения редко используемых записей»
    return re.match(r"\s*[—–]\s*[a-zа-яё]", after) is not None


def definition_at(text: str, abbreviation: str) -> tuple[str, tuple[int, int]] | None:
    """Расшифровка и положение конструкции, в которой она найдена."""
    escaped = re.escape(abbreviation)
    for match in re.finditer(rf"\(\s*{escaped}\s*[),;]", text):
        definition = _expansion_before(text[: match.start()], abbreviation)
        if definition:
            return definition, match.span()
    after = re.search(rf"(?<![\w-]){escaped}\s*\(([^)]{{3,{_MAX_DEFINITION}}})\)", text)
    if after and _LOWER.search(after.group(1)):
        return " ".join(after.group(1).split()), after.span()
    dash = re.search(rf"(?<![\w-]){escaped}\s*[—–]\s*([^.;\n]{{3,{_MAX_DEFINITION}}})", text)
    if dash and _LOWER.search(dash.group(1)[:1]):
        return " ".join(dash.group(1).split()), dash.span()
    return None


def _expansion_before(text: str, abbreviation: str) -> str | None:
    """Последние слова перед скобкой: столько, сколько букв в сокращении, начиная со слова
    на ту же букву (служебные слова внутри расшифровки допустимы)."""
    letters = sum(char.isalpha() for char in abbreviation)
    words = _WORD.findall(text)[-(letters + 3) :]
    if not words:
        return None
    first = abbreviation[0].casefold()
    starts = [index for index, word in enumerate(words) if word[0].casefold() == first]
    if not starts:
        return None
    start = min(starts, key=lambda index: abs((len(words) - index) - letters))
    definition = " ".join(words[start:])
    return definition if definition.casefold() != abbreviation.casefold() else None
