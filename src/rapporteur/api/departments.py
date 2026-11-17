"""HTTP endpoints for departments."""

import tempfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from rapporteur import (
    db,
    departments,
)
from rapporteur.api.dependencies import ReadTx, Tx, _database, _storage, _wake
from rapporteur.errors import NotFoundError, StageError, UserError
from rapporteur.ingest.inventory import (
    inspect_file,
)
from rapporteur.ingest.limits import check_size
from rapporteur.storage import sha256_hex

api = APIRouter()


@api.get("/departments")
def list_departments(conn: ReadTx) -> list[dict[str, object]]:
    return departments.latest(conn)


@api.post("/departments", status_code=201)
async def upload_department(
    request: Request,
    name: str = Query(min_length=1, max_length=200),
    filename: str = Query(min_length=1, max_length=255),
    department_id: str | None = None,
) -> dict[str, object]:
    if request.headers.get("content-type") != "application/octet-stream":
        raise UserError("Передайте файл как application/octet-stream", 415)
    if not name.strip():
        raise UserError("Укажите название отдела")
    content = bytearray()
    async for chunk in request.stream():
        check_size(len(content) + len(chunk), 200 * 2**20)
        content.extend(chunk)

    def save() -> dict[str, object]:
        from rapporteur.ingest.documents import pages

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("source" + PurePosixPath(filename).suffix)
            path.write_bytes(content)
            inspection = inspect_file(path)
        if inspection.media_type not in (
            "application/pdf",
            "text/plain",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ):
            raise UserError("Положение должно быть DOCX, DOC, PDF или TXT/MD")
        extracted = pages(bytes(content), inspection.media_type)
        stored_pages = [{"page": i, "text": text} for i, (text, _, _) in enumerate(extracted, 1)]
        if not any(text.strip() for text, _, _ in extracted):
            raise UserError("В положении не найден текст")
        sha = sha256_hex(bytes(content))
        from rapporteur.storage import blob_key

        _storage(request).put(blob_key(sha), bytes(content))
        with _database(request).tx() as conn:
            dep_id = department_id or db.new_id()
            if department_id and not db.one(
                conn, "SELECT 1 FROM departments WHERE id=?", department_id
            ):
                raise NotFoundError("Отдел не найден")
            number = conn.execute(
                "SELECT coalesce(max(version),0)+1 FROM departments WHERE id=?", (dep_id,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO departments VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dep_id, number, name.strip(), filename, sha, db.dumps(stored_pages), db.now()),
            )
            return next(d for d in departments.latest(conn) if d["id"] == dep_id)

    try:
        return await run_in_threadpool(save)
    except StageError as error:
        raise UserError(error.message, 422) from error


@api.get("/talks/{talk_id}/recommendations")
def get_recommendations(conn: ReadTx, talk_id: str) -> dict[str, object]:
    result = departments.view(conn, talk_id)
    job = db.one(conn, "SELECT job, error FROM recommendation_jobs WHERE talk_id=?", talk_id)
    return {**result, "job": dict(job) if job else None}


@api.post("/talks/{talk_id}/recommendations", status_code=202)
def recommend(request: Request, conn: Tx, talk_id: str) -> Response:
    departments.queue(conn, talk_id)
    _wake(request)
    return Response(status_code=202)


@api.put("/talks/{talk_id}/recommendations")
def review_recommendations(conn: Tx, talk_id: str, data: departments.Review) -> dict[str, object]:
    departments.review(conn, talk_id, data)
    return departments.view(conn, talk_id)
