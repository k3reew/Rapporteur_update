"""Выдача справки: DOCX по бланку, Markdown и PDF из того же DOCX (spec §6.9).

DOCX — единственный источник текста: Markdown строится из его абзацев, PDF — конвертацией
через LibreOffice, поэтому три формата не расходятся по содержанию.
"""

import io
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from docx.table import Table
from docx.text.paragraph import Paragraph

from rapporteur.contracts.enums import ReportFormat
from rapporteur.contracts.reports import ReportFile
from rapporteur.office import ConversionError, convert_to_pdf
from rapporteur.report.template import Template, render_docx
from rapporteur.storage import Storage, sha256_hex

PDF_TIMEOUT_S = 120
_FILE_NAMES = {
    ReportFormat.DOCX: "spravka.docx",
    ReportFormat.MARKDOWN: "spravka.md",
    ReportFormat.PDF: "spravka.pdf",
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]])")
_LINE_START_SPECIAL = re.compile(r"^(#|>|\+|-(?=\s))")


class ExportError(Exception):
    pass


def render_formats(
    template: Template,
    context: Mapping[str, Any],
    formats: Iterable[ReportFormat],
    work: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict[ReportFormat, bytes]:
    context = dict(context)
    operator = "sections" in context
    missing = "________________" if operator else "Автор не установлен"
    talks = {
        talk.talk_id: talk.model_copy(
            update={
                "speakers": talk.speakers or [missing],
                "authors": talk.authors or talk.speakers or [missing],
                # docxtpl uses the paragraph marker to preserve real Word paragraph boundaries.
                "summary": talk.summary.replace("\n\n", "\a") if talk.summary else None,
            }
        )
        for talk in context.get("talks", [])
    }
    context["talks"] = list(talks.values())
    if operator:
        context["sections"] = [
            section | {"talks": [talks[talk.talk_id] for talk in section["talks"]]}
            for section in context["sections"]
        ]
    if progress:
        progress("docx")
    docx = render_docx(template, context)
    # Add the appendix before deriving Markdown/PDF so all formats have identical text.
    recommendations = context.get("department_recommendations", [])
    if recommendations and not operator:
        document = Document(io.BytesIO(docx))

        def heading(text: str, *, centered: bool = False) -> None:
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.first_line_indent = Pt(0)
            paragraph.paragraph_format.space_before = Pt(12)
            paragraph.paragraph_format.keep_with_next = True
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER if centered else WD_ALIGN_PARAGRAPH.LEFT
            paragraph.add_run(text).bold = True

        heading("Доклады по интересам отделов", centered=True)
        talks = {str(t.talk_id): t for t in context["talks"]}
        for item in sorted(recommendations, key=lambda r: (r["department_name"], r["talk_id"])):
            talk = talks[item["talk_id"]]
            heading(item["department_name"])
            authors = (", ".join(talk.speakers) or "Автор не установлен").rstrip(".")
            document.add_paragraph(f"{talk.title.rstrip('.')}. Авторы: {authors}.")
            document.add_paragraph(item["reason"])
            document.add_paragraph(
                f"Положение: {item['regulation_name']}, версия {item['department_version']}."
            )
            for key, text in item["paragraphs"].items():
                document.add_paragraph(f"{key}: {text}")
            for key, text in item["facts"].items():
                document.add_paragraph(f"Факт доклада {key}: {text}")
        output = io.BytesIO()
        document.save(output)
        docx = output.getvalue()
    rendered: dict[ReportFormat, bytes] = {}
    for report_format in formats:
        if progress:
            progress(report_format.value)
        match report_format:
            case ReportFormat.DOCX:
                rendered[report_format] = docx
            case ReportFormat.MARKDOWN:
                rendered[report_format] = docx_to_markdown(docx).encode()
            case ReportFormat.PDF:
                rendered[report_format] = docx_to_pdf(docx, work)
    return rendered


def export_files(
    storage: Storage, directory: str, rendered: Mapping[ReportFormat, bytes]
) -> list[ReportFile]:
    """Файлы версии справки в каталоге тома (ADR-005) для ReportVersion.files."""
    files: list[ReportFile] = []
    for report_format, data in rendered.items():
        key = f"{directory}/{_FILE_NAMES[report_format]}"
        storage.put(key, data)
        files.append(
            ReportFile(
                format=report_format, storage_key=key, sha256=sha256_hex(data), size_bytes=len(data)
            )
        )
    return files


def docx_to_markdown(docx: bytes) -> str:
    """Сохраняет порядок абзацев и таблиц DOCX."""
    blocks: list[str] = []
    for element in Document(io.BytesIO(docx)).iter_inner_content():
        if isinstance(element, Table) and element.rows:
            rows = [
                [
                    _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(cell.text.split())).replace("|", r"\|")
                    for cell in row.cells
                ]
                for row in element.rows
            ]
            rows.insert(1, ["---"] * len(rows[0]))
            blocks.append("\n".join("| " + " | ".join(row) + " |" for row in rows))
            continue
        if not isinstance(element, Paragraph):
            continue
        text = element.text.strip()
        if not text:
            continue
        escaped = _LINE_START_SPECIAL.sub(r"\\\1", _MARKDOWN_SPECIAL.sub(r"\\\1", text))
        runs = [run for run in element.runs if run.text.strip()]
        if runs and all(run.bold for run in runs):
            escaped = f"**{escaped}**"
        blocks.append(escaped)
    return "\n\n".join(blocks) + "\n"


def docx_to_pdf(docx: bytes, work: Path) -> bytes:
    source = work / "spravka.docx"
    source.write_bytes(docx)
    try:
        return convert_to_pdf(source, work, "pdf:writer_pdf_Export", PDF_TIMEOUT_S).read_bytes()
    except ConversionError as error:
        raise ExportError(f"PDF не собран: {error}") from error
