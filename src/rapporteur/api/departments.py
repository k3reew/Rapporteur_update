"""HTTP endpoints for departments."""

import sqlite3
import tempfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
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
from rapporteur.storage import blob_key, sha256_hex

api = APIRouter()


@api.get("/departments")
def list_departments(conn: ReadTx) -> list[dict[str, object]]:
    return departments.latest(conn)


class DepartmentWrite(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    functions: str = Field(default="", max_length=20000)
    department_id: str | None = None


def _latest_source(conn: sqlite3.Connection, department_id: str | None) -> dict[str, str]:
    if department_id is None:
        return {
            "original_name": "",
            "sha256": "",
            "pages": "[]",
            "description": "",
            "functions": "",
        }
    row = db.one(
        conn,
        "SELECT d.original_name,d.sha256,d.pages,coalesce(x.description,'') AS description, "
        "coalesce(x.functions,'') AS functions FROM departments d LEFT JOIN department_details x "
        "ON x.department_id=d.id AND x.version=d.version WHERE d.id=? "
        "ORDER BY d.version DESC LIMIT 1",
        department_id,
    )
    if row is None:
        raise NotFoundError("Отдел не найден")
    return dict(row)


def _save_revision(
    conn: sqlite3.Connection,
    department_id: str | None,
    name: str,
    filename: str,
    sha: str,
    pages: str,
    description: str,
    functions: str,
) -> dict[str, object]:
    dep_id = department_id or db.new_id()
    current = next((d for d in departments.latest(conn) if d["id"] == dep_id), None)
    name, description, functions = name.strip(), description.strip(), functions.strip()
    if current and (name, filename, sha, description, functions) == tuple(
        current[k] for k in ("name", "original_name", "sha256", "description", "functions")
    ):
        return current
    number = int(current["version"]) + 1 if current else 1
    conn.execute(
        "INSERT INTO departments VALUES (?, ?, ?, ?, ?, ?, ?)",
        (dep_id, number, name, filename, sha, pages, db.now()),
    )
    conn.execute(
        "INSERT INTO department_details VALUES (?,?,?,?)",
        (dep_id, number, description, functions),
    )
    return next(d for d in departments.latest(conn) if d["id"] == dep_id)


@api.post("/departments", status_code=201)
async def upload_department(
    request: Request,
    name: str | None = Query(default=None, min_length=1, max_length=200),
    filename: str | None = Query(default=None, min_length=1, max_length=255),
    department_id: str | None = None,
) -> dict[str, object]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type == "application/json":
        from pydantic import ValidationError

        try:
            data = DepartmentWrite.model_validate(await request.json())
        except (ValidationError, ValueError) as error:
            raise UserError("Проверьте название, описание и функции отдела", 422) from error
        if not data.name.strip():
            raise UserError("Укажите название отдела")
        with _database(request).tx() as conn:
            previous = _latest_source(conn, data.department_id)
            if not (
                data.description.strip() or data.functions.strip() or previous["pages"] != "[]"
            ):
                raise UserError("Укажите функции, описание или загрузите положение отдела")
            return _save_revision(
                conn,
                data.department_id,
                data.name,
                previous["original_name"],
                previous["sha256"],
                previous["pages"],
                data.description,
                data.functions,
            )
    if content_type != "application/octet-stream":
        raise UserError(
            "Передайте карточку отдела в JSON или файл как application/octet-stream", 415
        )
    if not name or not name.strip() or not filename:
        raise UserError("Укажите название отдела и файла")
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
        _storage(request).put(blob_key(sha), bytes(content))
        with _database(request).tx() as conn:
            previous = _latest_source(conn, department_id)
            return _save_revision(
                conn,
                department_id,
                name,
                filename,
                sha,
                db.dumps(stored_pages),
                previous["description"],
                previous["functions"],
            )

    try:
        return await run_in_threadpool(save)
    except StageError as error:
        raise UserError(error.message, 422) from error


@api.get("/departments/{department_id}/source", response_class=FileResponse)
def department_source(
    request: Request, conn: ReadTx, department_id: str, version: int
) -> FileResponse:
    row = conn.execute(
        "SELECT original_name,sha256 FROM departments WHERE id=? AND version=?",
        (department_id, version),
    ).fetchone()
    if row is None or not row["sha256"]:
        raise NotFoundError("Файл положения отсутствует")
    source = _storage(request).path(blob_key(row["sha256"]))
    if not source.is_file():
        raise NotFoundError("Файл положения отсутствует")
    return FileResponse(source, filename=row["original_name"])


@api.delete("/departments/{department_id}", status_code=204)
def delete_department(conn: Tx, department_id: str) -> Response:
    departments.remove(conn, department_id)
    return Response(status_code=204)


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
