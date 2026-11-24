"""HTTP endpoints for files."""

import hashlib
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import anyio
import yaml
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from rapporteur import (
    db,
    departments,
    materials,
    preparation,
    processing,
)
from rapporteur.api.dependencies import ReadTx, Tx, _database, _storage, _wake
from rapporteur.contracts import SourceFile
from rapporteur.errors import FailureCode, NotFoundError, StageError, UserError
from rapporteur.ingest.inventory import (
    HEADER_BYTES,
    MAX_UPLOAD_BYTES,
    PPTX,
    inspect_file,
    require_supported_upload,
    upload_limit,
)
from rapporteur.ingest.limits import check_size

api = APIRouter()

_UPLOAD_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


@api.post("/conferences/{conference_id}/materials", status_code=201, openapi_extra=_UPLOAD_SCHEMA)
async def upload_material(
    request: Request,
    response: Response,
    conference_id: str,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    program: bool = False,
    purpose: Literal["presentation", "document", "notes", "program"] | None = None,
) -> SourceFile:
    """Файл потоком на том. Тип определяется по содержимому; повтор — ответ 200."""
    path = materials.safe_path(path)
    if request.headers.get("content-type") != "application/octet-stream":
        raise UserError("Передайте содержимое файла как application/octet-stream", 415)
    database = _database(request)
    storage = _storage(request)

    def require() -> None:
        with database.read() as conn:
            materials.require_conference(conn, conference_id)
            from rapporteur.departments import require_regulations

            require_regulations(conn)

    await run_in_threadpool(require)
    work = storage.root / "tmp"
    await anyio.Path(work).mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="upload-", dir=work) as tmp:
            staged = Path(tmp) / ("source" + PurePosixPath(path).suffix)
            digest = hashlib.sha256()
            size = 0
            limit = MAX_UPLOAD_BYTES
            head = bytearray()
            async with await anyio.open_file(staged, "wb") as stream:
                async for chunk in request.stream():
                    if len(head) < HEADER_BYTES:
                        head.extend(chunk[: HEADER_BYTES - len(head)])
                        if len(head) == HEADER_BYTES:
                            require_supported_upload(bytes(head))
                            limit = upload_limit(bytes(head))
                    size += len(chunk)
                    check_size(size, limit)
                    digest.update(chunk)
                    await stream.write(chunk)

            def register() -> tuple[SourceFile, bool]:
                require_supported_upload(bytes(head))
                name = PurePosixPath(path).name
                marked = program or PurePosixPath(name).stem.casefold() in {"программа", "program"}
                inspection = inspect_file(staged, program=marked or purpose == "program")
                if purpose is not None:
                    from rapporteur.contracts.enums import FileKind

                    if inspection.kind in (FileKind.AUDIO, FileKind.VIDEO, FileKind.IMAGE):
                        raise UserError("Для записи и изображения назначение определяется форматом")
                    if purpose == "presentation" and inspection.media_type not in (
                        "application/pdf",
                        PPTX,
                    ):
                        raise UserError("Презентация должна быть PPTX или PDF")
                    if purpose == "notes" and inspection.media_type != "text/plain":
                        raise UserError("Заметки слушателя должны быть TXT/MD")
                    if purpose == "document" and inspection.media_type == PPTX:
                        raise UserError("PPTX загружается как презентация")
                    from dataclasses import replace

                    inspection = replace(inspection, kind=FileKind(purpose))
                sha = digest.hexdigest()
                item = inspection.item(staged, name, sha)
                with database.tx() as conn:
                    result = materials.add_file(conn, conference_id, path, item, inspection)
                    storage.add_blob(sha, staged)
                    if result[1]:
                        preparation.materials_changed(conn, conference_id)
                return result

            source, created = await run_in_threadpool(register)
    except StageError as error:
        status = 413 if error.code is FailureCode.FILE_TOO_LARGE else 422
        raise UserError(error.message, status) from error
    if not created:
        response.status_code = 200
    return source


@api.get("/conferences/{conference_id}/materials")
def review_materials(conn: ReadTx, conference_id: str) -> materials.Materials:
    return materials.review(conn, conference_id)


@api.put("/conferences/{conference_id}/materials/bindings")
def confirm_materials(
    request: Request, conn: Tx, conference_id: str, data: materials.BindingReview
) -> materials.Materials:
    conference = materials.require_conference(conn, conference_id)
    if preparation.operator(conn, conference_id):
        raise UserError("Подтвердите привязки на экране «Реестр докладов»", 409)
    materials.confirm(conn, conference_id, data)
    # После запуска обработки новые и перепривязанные доклады обрабатываются сразу.
    if conference["processing_started"] and processing.enqueue_ready(conn, conference_id):
        _wake(request)
    return materials.review(conn, conference_id)


@api.get("/conferences/{conference_id}/conference.yaml", response_class=Response)
def export_registry(conn: ReadTx, conference_id: str) -> Response:
    registry = materials.registry(conn, conference_id)
    return Response(
        yaml.safe_dump(registry.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="conference.yaml"'},
    )


@api.get("/files/{file_id}/content")
def source_file(request: Request, conn: ReadTx, file_id: str) -> FileResponse:
    row = db.one(conn, "SELECT * FROM files WHERE id=?", file_id)
    if row is None:
        raise NotFoundError("Материал не найден")
    from rapporteur.storage import blob_key

    return FileResponse(
        _storage(request).path(blob_key(row["sha256"])),
        media_type=row["media_type"],
        filename=row["original_name"],
        content_disposition_type="inline",
    )


@api.get("/files/{file_id}/pages/{page_no}")
def document_page(request: Request, conn: ReadTx, file_id: str, page_no: int) -> Response:
    if page_no < 1 or page_no > 300:
        raise UserError("Недопустимый номер страницы")
    row = db.one(conn, "SELECT * FROM files WHERE id=?", file_id)
    if row is None:
        raise NotFoundError("Материал не найден")
    from rapporteur.contracts.enums import FileKind

    key = (
        processing.slide_image_key(row["sha256"], page_no)
        if row["kind"] == FileKind.PRESENTATION
        else f"pages/{row['sha256']}/page-{page_no:03}.png"
    )
    if not _storage(request).exists(key):
        raise NotFoundError("Страница ещё не подготовлена")
    return Response(_storage(request).get(key), media_type="image/png")


@api.get("/files/{file_id}/slides/{slide_no}", response_class=Response)
def slide_image(request: Request, conn: ReadTx, file_id: str, slide_no: int) -> Response:
    """Картинка слайда из разбора презентации: к ней ведут ссылки сводки."""
    row = db.one(conn, "SELECT sha256 FROM files WHERE id = ?", file_id)
    if row is None:
        raise NotFoundError("Файл не найден")
    storage = _storage(request)
    key = processing.slide_image_key(row["sha256"], slide_no)
    if not storage.exists(key):
        raise NotFoundError("Слайд ещё не разобран")
    return Response(
        storage.get(key), media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
    )


class PurposeWrite(BaseModel):
    purpose: Literal["presentation", "document", "notes", "program"]


@api.put("/files/{file_id}/purpose")
def change_purpose(request: Request, conn: Tx, file_id: str, data: PurposeWrite) -> Response:
    from rapporteur.contracts.enums import FileKind

    row = db.one(conn, "SELECT * FROM files WHERE id=?", file_id)
    if row is None:
        raise NotFoundError("Материал не найден")
    departments.require_regulations(conn)
    if row["kind"] in (FileKind.AUDIO, FileKind.VIDEO, FileKind.IMAGE):
        raise UserError("Для записи и изображения назначение определяется форматом")
    if data.purpose == "presentation" and row["media_type"] not in (PPTX, "application/pdf"):
        raise UserError("Презентация должна быть PPTX или PDF")
    if data.purpose == "notes" and row["media_type"] != "text/plain":
        raise UserError("Заметки должны быть TXT/MD")
    if data.purpose == "document" and row["media_type"] == PPTX:
        raise UserError("PPTX обрабатывается как презентация")
    affected = {
        r["talk_id"]
        for r in conn.execute("SELECT talk_id FROM material_links WHERE file_id=?", (file_id,))
    }
    if row["talk_id"]:
        affected.add(row["talk_id"])
    conn.execute(
        "UPDATE files SET kind=?, confirmed=0, talk_id=NULL WHERE id=?", (data.purpose, file_id)
    )
    conn.execute("DELETE FROM material_links WHERE file_id=?", (file_id,))
    for talk_id in affected:
        conn.execute(
            "UPDATE talks SET input_revision=input_revision+1, status=CASE WHEN status IN "
            "('processed','reviewed') THEN 'stale' ELSE status END WHERE id=?",
            (talk_id,),
        )
    preparation.materials_changed(conn, row["conference_id"])
    _wake(request)
    return Response(status_code=204)
