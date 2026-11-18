"""HTTP endpoints for reports."""

from urllib.parse import quote

from fastapi import APIRouter, Request, Response

from rapporteur import (
    report_jobs,
    reports,
)
from rapporteur.api.dependencies import ReadTx, Tx, _database, _storage, _wake
from rapporteur.contracts.enums import ReportFormat
from rapporteur.errors import UserError
from rapporteur.storage import sha256_hex

api = APIRouter()


@api.post("/conferences/{conference_id}/report-jobs", status_code=202)
def queue_report(
    request: Request, conference_id: str, data: report_jobs.BuildRequest
) -> dict[str, object]:
    result = report_jobs.enqueue(_database(request), _storage(request), conference_id, data)
    _wake(request)
    return result


@api.get("/conferences/{conference_id}/report-jobs")
def list_report_jobs(conn: ReadTx, conference_id: str) -> list[dict[str, object]]:
    return report_jobs.list_jobs(conn, conference_id)


@api.get("/report-jobs/{job_id}")
def report_job(conn: ReadTx, job_id: str) -> dict[str, object]:
    return report_jobs.view(conn, job_id)


@api.post("/report-jobs/{job_id}/retry", status_code=202)
def retry_report(request: Request, conn: Tx, job_id: str) -> dict[str, object]:
    result = report_jobs.retry(conn, job_id)
    _wake(request)
    return result


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
