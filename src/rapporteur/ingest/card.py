"""Карточка доклада без модели: контакты и организации с титульной и последней страницы.

Шаблоны надёжно находят e-mail, телефоны, сайты и ORCID. Организация определяется по признакам
в короткой строке (университет, институт, АО…), поэтому смотрятся только титульная и последняя
страницы, без строки с темой. Контакт или организация относятся к автору, если стоят в одной
строке с его фамилией, если e-mail содержит фамилию латиницей или если автор один. Остальное
остаётся сведениями доклада без привязки к автору: их распределит модель или оператор.
"""

import re
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, Field

from rapporteur.text import normalize_text

VERSION = "1.0.0"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[^\W\d_]{2,}")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"«»]+|\bt\.me/\w+", re.IGNORECASE)
_PHONE = re.compile(
    r"(?<![\w+])(?:\+\d{1,3}|8)[\s -]*\(?\d{3,4}\)?[\s -]*\d{2,3}"
    r"[\s -]*\d{2}[\s -]*\d{2}(?!\d)"
)
_ORCID = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")
_ORGANIZATION_WORDS = re.compile(
    r"университет|институт|академи[яиюейь]|кафедр|факультет|лаборатори|корпораци|концерн|"
    r"компани[яиюей]|предприяти|завод|министерств|агентств|"
    r"universit|institut|laborator|academy|corporation|company|\b(?:inc|ltd|llc|gmbh)\b",
    re.IGNORECASE,
)
# Аббревиатуры и «Центр» — с учётом регистра: «центр» в тексте слайда — не организация.
_ORGANIZATION_MARKS = re.compile(
    r"\b(?:НИИ|НИЦ|ЦНИИ|РАН|ГНЦ|ФИЦ|ФГУП|ФГБУ|ФГБОУ|ФГАОУ|ФГБНУ|ГБУ|АНО|АО|ПАО|ОАО|ЗАО|ООО|"
    r"НПО|НПП|ОКБ|МГТУ|МГУ|МФТИ|МИФИ|НИЯУ|ВШЭ|НИУ|СПбГУ|ИТМО|Центр|Center|Centre)\b"
)
_FOOTNOTE = re.compile(r"^[\s\d¹²³⁴⁵⁶⁷⁸⁹⁰*†‡,.)]+|[\s\d¹²³⁴⁵⁶⁷⁸⁹⁰*†‡]+$")
_LABEL = re.compile(
    r"\b(?:e-?mail|эл\.?\s*почта|тел(?:ефон)?|phone|сайт|web|orcid)\b\.?\s*:?", re.IGNORECASE
)
# Инициалы, оставшиеся в начале строки после удаления фамилии: «И. И., МГТУ».
_LEADING_INITIALS = re.compile(r"^\s*(?:[A-ZА-ЯЁ]\.\s*){1,3}[,;–-]?\s*")
_MAX_LINE = 160
_MAX_WORDS = 20
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip
# Распространённые варианты латиницы фамилий: Khabarov/Habarov, Tsoy/Coy, Yuriev/Iuriev.
_ALTERNATIVES = {"kh": "h", "ts": "c", "shch": "sch", "yu": "iu", "ya": "ia", "zh": "j"}


class CardHints(BaseModel):
    """Найденное на страницах. affiliations и speaker_contacts — по имени автора из списка;
    organizations и contacts — не отнесённые ни к кому."""

    organizations: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)
    affiliations: dict[str, str] = Field(default_factory=dict)
    speaker_contacts: dict[str, list[str]] = Field(default_factory=dict)


def find_contacts(text: str) -> list[str]:
    """E-mail, сайты, телефоны и ORCID в порядке появления, без повторов."""
    return _unique(value for _, _, value in _contact_spans(text))


def _contact_spans(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []

    def add(match: re.Match[str], value: str) -> None:
        if not any(start < match.end() and match.start() < end for start, end, _ in found):
            found.append((match.start(), match.end(), value))

    for match in _EMAIL.finditer(text):
        add(match, match.group(0).lower())
    for match in _URL.finditer(text):
        add(match, match.group(0).rstrip(".,;:)"))
    for match in _ORCID.finditer(text):
        add(match, "ORCID " + match.group(0))
    for match in _PHONE.finditer(text):
        add(match, " ".join(match.group(0).split()))
    return sorted(found)


def is_organization(line: str) -> bool:
    return bool(_ORGANIZATION_WORDS.search(line) or _ORGANIZATION_MARKS.search(line))


def card_hints(
    lines: Iterable[str], speakers: Sequence[str], title: str | None = None
) -> CardHints:
    """Сведения карточки из строк титульной и последней страниц."""
    hints = CardHints()
    title_key = _key(title or "")
    surnames = {name: _surnames(name) for name in speakers}
    loose_contacts: list[str] = []
    loose_organizations: list[str] = []
    for raw in lines:
        line = " ".join(normalize_text(raw).split())
        line_key = _key(line)
        if not line or (title_key and line_key and line_key in title_key):
            continue
        spans = _contact_spans(line)
        contacts = _unique(value for _, _, value in spans)
        rest = line
        for start, end, _ in reversed(spans):
            rest = rest[:start] + " " + rest[end:]
        owners = [name for name, keys in surnames.items() if keys & set(_words(rest))]
        for name in owners:
            for key in surnames[name]:
                rest = re.sub(rf"\b{re.escape(key)}\b", " ", rest, flags=re.IGNORECASE)
        organization = _organization(rest)
        if len(owners) == 1:
            owner = owners[0]
            hints.speaker_contacts.setdefault(owner, []).extend(contacts)
            if organization:
                hints.affiliations.setdefault(owner, organization)
            continue
        loose_contacts.extend(contacts)
        if organization:
            loose_organizations.append(organization)

    for contact in _unique(loose_contacts):
        holder = _email_owner(contact, surnames) if "@" in contact else None
        if holder is None and len(speakers) == 1:
            holder = speakers[0]
        if holder is None:
            hints.contacts.append(contact)
        else:
            hints.speaker_contacts.setdefault(holder, []).append(contact)
    organizations = _unique(loose_organizations)
    if len(speakers) == 1 and organizations and speakers[0] not in hints.affiliations:
        hints.affiliations[speakers[0]] = "; ".join(organizations)
    else:
        known = set(hints.affiliations.values())
        hints.organizations = [item for item in organizations if item not in known]
    hints.speaker_contacts = {
        name: _unique(values) for name, values in hints.speaker_contacts.items() if values
    }
    return hints


class CardAuthor(BaseModel):
    name: str
    organizations: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)


class CardAnswer(BaseModel):
    """Ответ модели: организации и контакты по авторам и не отнесённые ни к кому."""

    authors: list[CardAuthor] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)


CARD_INSTRUCTION = (
    "Определи организации и контакты авторов доклада по его титульной и последней страницам. "
    "Для каждого автора из списка укажи организации и контакты, только если связь видна: "
    "та же строка, одинаковые сноски ¹ ² или * у фамилии и организации, e-mail с фамилией. "
    "Копируй организации и контакты дословно, без знаков сносок. Тема доклада, должности "
    "и учёные степени организациями не являются. Организации и контакты, которые нельзя "
    "уверенно отнести к автору, перечисли в organizations и contacts доклада."
)


def needs_model(
    lines: Sequence[str], speakers: Sequence[str], missing: Sequence[str], hints: CardHints
) -> bool:
    """Модель нужна, если шаблоны не смогли распределить организации между авторами без
    организации или если похожие на организации строки (кавычки, аббревиатуры) не
    распознаны."""
    if not missing:
        return False
    if len(speakers) > 1 and (hints.organizations or hints.contacts):
        return True
    if hints.organizations or hints.affiliations:
        return False
    surnames = {key for name in speakers for key in _surnames(name)}
    for raw in lines:
        line = " ".join(normalize_text(raw).split())
        rest = line
        for start, end, _ in reversed(_contact_spans(line)):
            rest = rest[:start] + " " + rest[end:]
        if surnames & set(_words(rest)) or len(rest) > _MAX_LINE:
            continue
        if "«" in rest or '"' in rest or re.search(r"\b[A-ZА-ЯЁ]{2,}\b", rest):
            return True
    return False


def card_prompt(lines: Sequence[str], speakers: Sequence[str], limit: int = 6000) -> str:
    text = "\n".join(" ".join(line.split()) for line in lines if line.strip())
    return "Авторы: " + "; ".join(speakers) + "\nСтраницы:\n" + text[:limit]


def grounded(answer: CardAnswer, lines: Sequence[str], speakers: Sequence[str]) -> CardHints:
    """Принимает из ответа модели только то, что есть на страницах: организации — по словам
    текста, контакты — среди найденных шаблонами, авторов — из списка."""
    page = " " + _key(" ".join(lines)) + " "
    known_contacts = {_contact_key(item): item for item in find_contacts("\n".join(lines))}

    def organizations(values: Iterable[str]) -> list[str]:
        result = []
        for value in values:
            value = _FOOTNOTE.sub("", " ".join(value.split())).strip(" ,;:–-")
            key = _key(value)
            if key and f" {key} " in page and len(value) <= _MAX_LINE:
                result.append(value)
        return _unique(result)

    def contacts(values: Iterable[str]) -> list[str]:
        return _unique(
            known_contacts[_contact_key(value)]
            for value in values
            if _contact_key(value) in known_contacts
        )

    hints = CardHints()
    surnames = {name: _surnames(name) for name in speakers}
    for author in answer.authors:
        owners = [name for name, keys in surnames.items() if keys & set(_words(author.name))]
        if len(owners) != 1:
            continue
        found = organizations(author.organizations)
        if found:
            hints.affiliations.setdefault(owners[0], "; ".join(found))
        if contacts(author.contacts):
            hints.speaker_contacts.setdefault(owners[0], contacts(author.contacts))
    hints.organizations = organizations(answer.organizations)
    hints.contacts = contacts(answer.contacts)
    return hints


def merge(rules: CardHints, model: CardHints) -> CardHints:
    """Шаблоны важнее модели; модель дополняет авторов без организации и контактов."""
    affiliations = model.affiliations | rules.affiliations
    # Контакт, отнесённый шаблонами к автору, модель другому не передаёт.
    taken = {item for values in rules.speaker_contacts.values() for item in values}
    speaker_contacts = {
        name: kept
        for name, values in model.speaker_contacts.items()
        if (kept := [item for item in values if item not in taken])
    } | rules.speaker_contacts
    attributed = {item for values in speaker_contacts.values() for item in values}
    assigned = {part.strip() for value in affiliations.values() for part in value.split(";")}
    return CardHints(
        affiliations=affiliations,
        speaker_contacts=speaker_contacts,
        organizations=[
            item
            for item in _unique([*rules.organizations, *model.organizations])
            if item not in assigned
        ],
        contacts=[
            item for item in _unique([*rules.contacts, *model.contacts]) if item not in attributed
        ],
    )


def _contact_key(value: str) -> str:
    value = value.removeprefix("ORCID ").casefold()
    digits = re.sub(r"\D", "", value)
    return digits if "@" not in value and "." not in value and len(digits) >= 10 else value.strip()


def _organization(line: str) -> str | None:
    line = _LABEL.sub(" ", line)
    line = _LEADING_INITIALS.sub("", " ".join(line.split()))
    line = _FOOTNOTE.sub("", line).strip(" ,;:–-")
    if not line or len(line) > _MAX_LINE or len(line.split()) > _MAX_WORDS:
        return None
    return line if is_organization(line) else None


def _email_owner(email: str, surnames: dict[str, set[str]]) -> str | None:
    local = re.sub(r"[^a-z]", "", email.split("@", 1)[0])
    owners = [
        name
        for name, keys in surnames.items()
        if any(variant in local for key in keys for variant in _latin(key))
    ]
    return owners[0] if len(owners) == 1 else None


def _latin(word: str) -> set[str]:
    base = "".join(_TRANSLIT.get(char, char) for char in word.casefold())
    variants = {base}
    for source, target in _ALTERNATIVES.items():
        variants |= {variant.replace(source, target) for variant in variants}
    return {variant for variant in variants if len(variant) >= 4 and variant.isascii()}


def _surnames(name: str) -> set[str]:
    """Слова имени длиной от трёх букв: фамилия и полные имена, без инициалов."""
    return {word for word in _words(name) if len(word) >= 3}


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text.casefold().replace("ё", "е"))


def _key(text: str) -> str:
    return " ".join(_words(text))


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
