"""Сборка справки функцией build_report: снимок входа, переменные и файлы (spec §6.9)."""

from pathlib import Path

import pytest

from rapporteur.contracts.enums import ReportFormat
from rapporteur.report.assemble import ReportError, select_talks
from rapporteur.report.build import REPORT_FORMATS, build_report
from rapporteur.storage import Storage, sha256_hex

from .test_report_build import SOURCES, SUMMARIES

# PDF проверяет test_report_build; здесь достаточно форматов без LibreOffice.
FAST = (ReportFormat.DOCX, ReportFormat.MARKDOWN)


def test_report_carries_the_input_snapshot_and_files(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data")
    selection = select_talks(SOURCES)

    built = build_report(SOURCES, selection, True, storage, "reports/v1", tmp_path, formats=FAST)

    included = {talk.talk_id for talk in built.input.talks}
    assert included == {item.talk_id for item in SUMMARIES}
    assert included | {talk.talk_id for talk in built.input.skipped} == set(selection.talk_ids)
    assert built.variables.talks_count == len(built.input.talks)
    assert [file.format for file in built.files] == list(FAST)
    for file in built.files:
        assert sha256_hex(storage.get(file.storage_key)) == file.sha256
    assert any("не проверена" in warning for warning in built.warnings)


def test_unreviewed_talks_need_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="непроверенные"):
        build_report(
            SOURCES, select_talks(SOURCES), False, Storage(tmp_path), "r", tmp_path, formats=FAST
        )


def test_broken_template_is_a_report_error(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        build_report(
            SOURCES,
            select_talks(SOURCES),
            True,
            Storage(tmp_path),
            "r",
            tmp_path,
            template_dir=tmp_path / "missing",
            formats=FAST,
        )


def test_all_three_formats_by_default() -> None:
    assert set(REPORT_FORMATS) == {ReportFormat.DOCX, ReportFormat.MARKDOWN, ReportFormat.PDF}


def test_operator_template_preserves_card_details_and_contact_table(tmp_path: Path) -> None:
    from dataclasses import replace

    from rapporteur.contracts import Speaker
    from rapporteur.contracts.enums import TalkStatus
    from rapporteur.contracts.reports import ReportContact, ReportDraftFields

    original = SOURCES.talks[0]
    card = original.model_copy(
        update={
            "status": TalkStatus.PROCESSED,
            "card_only": True,
            "organizations": ["Институт примера."],
            "speakers": [
                Speaker(
                    name="Иван Иванов",
                    affiliation="Институт примера",
                    position="Руководитель направления",
                    contacts=["test@example.invalid"],
                )
            ],
        }
    )
    section = SOURCES.section_by_id[card.section_id].model_copy(update={"date": None})
    sources = replace(
        SOURCES,
        conference=SOURCES.conference.model_copy(update={"start_date": None, "end_date": None}),
        talks=[card],
        sections=[section],
        operator_fields=ReportDraftFields(
            participation="Делегация лаборатории",
            contacts=[ReportContact(person="Мария Сергеева", cooperation="Обмен материалами")],
        ),
    )
    storage = Storage(tmp_path / "data")
    built = build_report(
        sources, select_talks(sources), False, storage, "report", tmp_path, formats=FAST
    )
    md = storage.get(
        next(file.storage_key for file in built.files if file.format == ReportFormat.MARKDOWN)
    ).decode()
    assert built.input.period_start is None
    assert "Делегация лаборатории" in md
    assert md.count("Институт примера") == 1
    assert "Руководитель направления" in md
    assert "test@example.invalid" in md
    assert "| Организация | Должность | ФИО / телефон | Доклад | Совместная работа |" in md
    assert "|  |  | Мария Сергеева |  | Обмен материалами |" in md
    assert "Основные выводы" not in md
