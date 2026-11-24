"""Инвентаризация оригинала без рендера и без загрузки записи в память."""

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from rapporteur.contracts.artifacts import InventoryItem, TitleCandidates
from rapporteur.contracts.enums import FileKind
from rapporteur.errors import FailureCode, StageError
from rapporteur.ingest.limits import (
    MIB,
    NotesConfig,
    PresentationConfig,
    check_size,
    check_zip,
)
from rapporteur.ingest.notes import decode_notes
from rapporteur.ingest.pdf_reader import open_pdf, read_page
from rapporteur.ingest.pptx_reader import read_pptx
from rapporteur.ingest.titles import pdf_title, pptx_title

MAX_UPLOAD_BYTES = 10 * 1024 * MIB
HEADER_BYTES = 1024
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_PDF_LOCK = Lock()  # PDFium не допускает параллельных вызовов из разных потоков.
_PROGRAMS = {
    "word/document.xml": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xl/workbook.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True)
class Inspection:
    kind: FileKind
    media_type: str
    title: TitleCandidates | None = None
    duration_ms: int | None = None
    encoding: str | None = None
    warning: str | None = None

    def item(self, path: Path, original_name: str, sha256: str) -> InventoryItem:
        return InventoryItem(
            kind=self.kind,
            media_type=self.media_type,
            size_bytes=path.stat().st_size,
            original_name=original_name,
            sha256=sha256,
            duration_ms=self.duration_ms,
            encoding=self.encoding,
        )


def upload_limit(head: bytes) -> int:
    if b"%PDF-" in head or head.startswith(b"PK\x03\x04"):
        return PresentationConfig().max_file_bytes
    return (
        NotesConfig().max_file_bytes
        if _looks_like_text(head)
        else PresentationConfig().max_file_bytes
    )


def require_supported_upload(head: bytes) -> None:
    if _looks_like_recording(head):
        raise StageError(
            FailureCode.UNSUPPORTED_FORMAT,
            "Аудио и видео в этой версии не обрабатываются. Загрузите текст или презентацию.",
        )


def inspect_file(path: Path, *, program: bool = False) -> Inspection:
    size = path.stat().st_size
    check_size(size, MAX_UPLOAD_BYTES)
    with path.open("rb") as stream:
        head = stream.read(HEADER_BYTES)
    presentation = PresentationConfig()
    if b"%PDF-" in head:
        check_size(size, presentation.max_file_bytes)
        with _PDF_LOCK, open_pdf(path) as pdf:
            count = len(pdf)
            if not count:
                raise StageError(FailureCode.BROKEN_FILE, "В PDF нет страниц")
            if count > presentation.max_pages:
                raise StageError(
                    FailureCode.TOO_MANY_PAGES,
                    f"В PDF {count} страниц при лимите {presentation.max_pages}",
                )
            title = None if program else pdf_title(read_page(pdf, 1))
        kind = FileKind.PROGRAM if program else FileKind.PRESENTATION
        return Inspection(kind, "application/pdf", title=title)
    if head.startswith(b"PK\x03\x04"):
        check_size(size, presentation.max_file_bytes)
        return _office(path, presentation, program=program)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        check_size(size, presentation.max_file_bytes)
        if path.suffix.casefold() == ".doc":
            return Inspection(
                FileKind.PROGRAM if program else FileKind.DOCUMENT, "application/msword"
            )
        raise StageError(
            FailureCode.UNSUPPORTED_FORMAT,
            "Старые и защищённые форматы Office: сохраните файл как pptx или pdf",
        )
    if path.suffix.casefold() == ".doc" and head.startswith(b"\xd0\xcf\x11\xe0"):
        return Inspection(FileKind.PROGRAM if program else FileKind.DOCUMENT, "application/msword")
    require_supported_upload(head)
    if _looks_like_text(head):
        check_size(size, NotesConfig().max_file_bytes)
        decoded = decode_notes(path.read_bytes())
        if _looks_like_text(decoded.text.encode("utf-8")) and decoded.text.strip():
            return Inspection(
                FileKind.PROGRAM if program else FileKind.NOTES,
                "text/plain",
                encoding=decoded.encoding,
                warning=None if decoded.confident else "Кодировка определена неуверенно",
            )
    from PIL import Image, UnidentifiedImageError

    try:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            pass
        with Image.open(path) as image:
            if image.width * image.height > presentation.max_render_pixels:
                raise StageError(FailureCode.FILE_TOO_LARGE, "Слишком большое изображение")
            media = Image.MIME.get(image.format or "", "image/heif")
            image.verify()
        check_size(size, presentation.max_file_bytes)
        return Inspection(FileKind.IMAGE, media)
    except (UnidentifiedImageError, OSError, ValueError):
        raise StageError(FailureCode.UNSUPPORTED_FORMAT, "Формат файла не поддерживается") from None


def _office(path: Path, config: PresentationConfig, *, program: bool = False) -> Inspection:
    check_zip(path, config)
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
    if "ppt/presentation.xml" in names:
        slides = read_pptx(path, max_pages=config.max_pages, first_only=True)
        if not slides:
            raise StageError(FailureCode.BROKEN_FILE, "В презентации нет слайдов")
        return Inspection(FileKind.PRESENTATION, PPTX, title=pptx_title(slides[0]))
    for entry, media_type in _PROGRAMS.items():
        if entry in names:
            return Inspection(
                FileKind.DOCUMENT
                if entry == "word/document.xml" and not program
                else FileKind.PROGRAM,
                media_type,
            )
    raise StageError(FailureCode.UNSUPPORTED_FORMAT, "Формат архива не поддерживается")


def _looks_like_text(data: bytes) -> bool:
    return data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) or all(
        (byte >= 32 and byte != 127) or byte in (9, 10, 13) for byte in data
    )


def _looks_like_recording(head: bytes) -> bool:
    return (
        head.startswith((b"ID3", b"OggS", b"fLaC", b"\x1aE\xdf\xa3", b"#!AMR", b"caff"))
        or (head.startswith((b"RIFF", b"RF64")) and head[8:12] in (b"WAVE", b"AVI "))
        or (head.startswith(b"FORM") and head[8:12] in (b"AIFF", b"AIFC"))
        or head[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free"}
        or (len(head) >= 2 and head[0] == 255 and head[1] & 224 == 224)
    )
