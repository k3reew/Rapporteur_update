"""HTTP endpoints for reports."""

from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from rapporteur import (
    reports,
)
from rapporteur.api.dependencies import ReadTx, _database, _storage
from rapporteur.contracts.enums import ReportFormat
from rapporteur.errors import UserError
from rapporteur.storage import sha256_hex

api = APIRouter()

_MEDIA = {
    ReportFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ReportFormat.MARKDOWN: "text/markdown; charset=utf-8",
    ReportFormat.PDF: "application/pdf",
}


@api.get("/conferences/{conference_id}/reports")
def list_reports(conn: ReadTx, conference_id: str) -> list[reports.ReportVersionView]:
    return reports.versions(conn, conference_id)


@api.post("/conferences/{conference_id}/reports", status_code=201)
def build_report(
    request: Request, conference_id: str, data: reports.ReportBuild
) -> reports.ReportVersionView:
    return reports.build(_database(request), _storage(request), conference_id, data)


@api.get("/report-versions/{version_id}/files/{report_format}", response_class=Response)
def report_file(
    request: Request, conn: ReadTx, version_id: str, report_format: ReportFormat
) -> Response:
    number, file = reports.report_file(conn, version_id, report_format.value)
    data = _storage(request).get(file.storage_key)
    if sha256_hex(data) != file.sha256:
        raise UserError("Файл справки повреждён", 500)
    name = f"Справка, версия {number}.{report_format.value}"
    return Response(
        data,
        media_type=_MEDIA[report_format],
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )
