"""Local document extraction and OCR; page numbers refer to the stored rendered PDF."""

import io
import subprocess
import tempfile
import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from rapporteur.contracts import FragmentDraft
from rapporteur.contracts.enums import FragmentKind, TextOrigin
from rapporteur.contracts.fragments import PageLocator
from rapporteur.errors import FailureCode, StageError
from rapporteur.ingest.limits import PresentationConfig, check_size, check_zip
from rapporteur.ingest.pdf_reader import open_pdf, read_page
from rapporteur.ingest.render import render_page
from rapporteur.office import ConversionError, convert_to_pdf
from rapporteur.text import normalize_text

VERSION = "1.0.0"


def image_pages(data: bytes) -> list[bytes]:
    # HEIF support is optional in development, installed in the production image.
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    pages = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                for index in range(getattr(image, "n_frames", 1)):
                    if index >= 300:
                        raise ValueError("Больше 300 страниц изображения")
                    image.seek(index)
                    if image.width * image.height > 40_000_000:
                        raise ValueError("Изображение больше 40 млн пикселей")
                    frame = ImageOps.exif_transpose(image).convert("RGB")
                    frame.thumbnail((2400, 2400))
                    out = io.BytesIO()
                    frame.save(out, format="PNG")
                    pages.append(out.getvalue())
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        raise StageError(FailureCode.BROKEN_FILE, f"Изображение не читается: {error}") from error
    return pages


def ocr(image: bytes, work: Path) -> str:
    source = work / "ocr.png"
    source.write_bytes(image)
    try:
        result = subprocess.run(
            ["tesseract", str(source), "stdout", "-l", "rus+eng"],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise StageError(
            FailureCode.RENDER_FAILED, "Ошибка OCR: проверьте Tesseract rus+eng"
        ) from error
    return result.stdout.decode("utf-8").strip()


def pages(data: bytes, media_type: str) -> list[tuple[str, bytes, TextOrigin]]:
    check_size(len(data), PresentationConfig().max_file_bytes)
    with tempfile.TemporaryDirectory(prefix="document-") as tmp:
        work = Path(tmp)
        if media_type.startswith("image/"):
            return [(ocr(image, work), image, TextOrigin.OCR) for image in image_pages(data)]
        if media_type == "text/plain":
            from rapporteur.ingest.notes import decode_notes

            text = decode_notes(data).text
            return [(text, b"", TextOrigin.EXTRACTED)]
        if media_type == "application/pdf":
            source = work / "source.pdf"
            source.write_bytes(data)
        else:
            suffix = ".doc" if media_type == "application/msword" else ".docx"
            source = work / ("source" + suffix)
            source.write_bytes(data)
            if suffix == ".docx":
                check_zip(source, PresentationConfig())
            try:
                source = convert_to_pdf(source, work, "pdf:writer_pdf_Export", 180)
            except ConversionError as error:
                raise StageError(FailureCode.RENDER_FAILED, str(error)) from error
        with open_pdf(source) as pdf:
            if not 0 < len(pdf) <= 300:
                raise StageError(
                    FailureCode.TOO_MANY_PAGES, "Документ должен содержать 1–300 страниц"
                )
            result = []
            for number in range(1, len(pdf) + 1):
                text = read_page(pdf, number).text
                image = render_page(pdf, number, PresentationConfig())
                origin = TextOrigin.EXTRACTED
                if not text.strip():
                    text, origin = ocr(image, work), TextOrigin.OCR
                result.append((text, image, origin))
            return result


def fragment(
    sha: str, number: int, text: str, origin: TextOrigin, kind: FragmentKind = FragmentKind.DOCUMENT
) -> FragmentDraft:
    return FragmentDraft(
        kind=kind,
        source_sha256=sha,
        locator=PageLocator(kind="page", page_no=number),
        raw_text=text,
        normalized_text=normalize_text(text),
        origin=origin,
        engine_confidence=None,
    )
