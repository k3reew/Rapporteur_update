"""Пакет фрагментов одного доклада с метками (spec §6.7, ADR-003, ADR-013).

Пакет строится из разбора презентаций и заметок доклада. Идентификаторы выводятся из данных:
файл — из конференции и SHA-256 (source_file_id), фрагмент — из файла и локатора, ревизия —
из фрагмента и текста. Поэтому повторная сборка даёт тот же пакет и тот же ключ кэша шагов
сводки, а id совпадают с записями БД.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from rapporteur.contracts import Evidence, FragmentDraft, PackedFragment, TalkFragmentPack
from rapporteur.contracts.claims import EvidenceDraft
from rapporteur.contracts.enums import FragmentKind, PackBlock
from rapporteur.contracts.fragments import PageLocator
from rapporteur.summarize.quotes import QuoteConfig, match_quote

_SLIDE_PREFIX = {
    FragmentKind.SLIDE: "S",
    FragmentKind.SLIDE_DESCRIPTION: "V",
    FragmentKind.DOCUMENT: "D",
}


@dataclass(frozen=True)
class PackSource:
    """Фрагменты одного файла доклада и id этого файла в конференции."""

    source_file_id: uuid.UUID
    fragments: Sequence[FragmentDraft]


def bounded_fragment(
    fragment: PackedFragment, evidence: Collection[Evidence | EvidenceDraft]
) -> PackedFragment:
    """Окна нужны только запросу. Цитаты сверяются с каноническим пакетом."""
    if len(fragment.text) <= 6000:
        return fragment
    spans = [(0, 400)]
    for item in evidence:
        if item.label != fragment.label:
            continue
        matched = match_quote(item.quote, fragment.text, QuoteConfig())
        if matched.span is not None:
            start, end = matched.span
            spans.append((max(0, start - 600), min(len(fragment.text), end + 600)))
    if len(spans) == 1:
        spans.append((max(400, len(fragment.text) - 1200), len(fragment.text)))
    parts = []
    previous = 0
    for start, end in sorted(set(spans)):
        start = max(start, previous)
        if start >= end:
            continue
        if start > previous:
            parts.append("\n[Пропуск контекста]\n")
        parts.append(fragment.text[start:end])
        previous = end
    return fragment.model_copy(update={"text": "".join(parts)})


def context_fragments(
    pack: TalkFragmentPack, evidence: Collection[Evidence | EvidenceDraft]
) -> list[PackedFragment]:
    """Соседние сегменты сохраняют оговорку, отделённую ASR от цитируемой фразы."""
    labels = {item.label for item in evidence}
    selected = set(labels)
    for index, fragment in enumerate(pack.fragments):
        if fragment.label not in labels or fragment.kind is not FragmentKind.TRANSCRIPT:
            continue
        for neighbor in pack.fragments[max(0, index - 1) : index + 2]:
            if (
                neighbor.kind is FragmentKind.TRANSCRIPT
                and neighbor.source_file_id == fragment.source_file_id
                and neighbor.block is fragment.block
            ):
                selected.add(neighbor.label)
    return [bounded_fragment(f, evidence) for f in pack.fragments if f.label in selected]


def build_pack(
    conference_id: uuid.UUID,
    talk_id: uuid.UUID,
    presentations: Sequence[PackSource],
    notes: Sequence[PackSource] = (),
    transcripts: Sequence[PackSource] = (),
) -> TalkFragmentPack:
    """S12 — слайд 12; при нескольких презентациях S2.12. N — заметки подряд по всем файлам.
    Фрагменты без текста в пакет не входят: на них нельзя сослаться."""
    packed: list[PackedFragment] = []
    numbered = len(presentations) > 1
    for index, source in enumerate(presentations, start=1):
        for draft in source.fragments:
            if draft.kind not in _SLIDE_PREFIX or not draft.normalized_text.strip():
                continue
            assert isinstance(draft.locator, PageLocator)
            prefix = _SLIDE_PREFIX[draft.kind]
            place = f"{index}.{draft.locator.page_no}" if numbered else str(draft.locator.page_no)
            packed.append(_packed(talk_id, source, draft, f"{prefix}{place}"))
    note_no = 0
    for source in notes:
        for draft in source.fragments:
            if draft.kind is not FragmentKind.NOTE or not draft.normalized_text.strip():
                continue
            note_no += 1
            packed.append(_packed(talk_id, source, draft, f"N{note_no}"))
    from rapporteur.contracts.fragments import TimeLocator, _timecode

    for index, source in enumerate(transcripts, 1):
        for draft in source.fragments:
            if draft.kind is not FragmentKind.TRANSCRIPT or not draft.normalized_text.strip():
                continue
            assert isinstance(draft.locator, TimeLocator)
            label = f"T{index}.{_timecode(draft.locator.start_ms)}"
            packed.append(_packed(talk_id, source, draft, label))
    return TalkFragmentPack(conference_id=conference_id, talk_id=talk_id, fragments=packed)


def _packed(
    talk_id: uuid.UUID, source: PackSource, draft: FragmentDraft, label: str
) -> PackedFragment:
    # Фрагмент принадлежит файлу, а не докладу: перепривязка файла его id не меняет.
    fragment_id = uuid.uuid5(
        source.source_file_id, f"{draft.kind.value}:{draft.locator.model_dump_json()}"
    )
    return PackedFragment(
        label=label,
        kind=draft.kind,
        fragment_id=fragment_id,
        revision_id=uuid.uuid5(fragment_id, draft.normalized_text),
        source_file_id=source.source_file_id,
        locator=draft.locator,
        origin=draft.origin,
        text=draft.normalized_text,
        block=PackBlock.MAIN,
        low_quality=False,
    )
