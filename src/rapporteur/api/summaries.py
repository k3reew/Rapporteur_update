"""HTTP endpoints for summaries."""

from fastapi import APIRouter

from rapporteur import (
    summaries,
)
from rapporteur.api.dependencies import ReadTx, Tx

api = APIRouter()


@api.get("/talks/{talk_id}/summary")
def get_summary(conn: ReadTx, talk_id: str) -> summaries.TalkSummaryView:
    return summaries.view(conn, talk_id)


@api.put("/talks/{talk_id}/summary")
def edit_summary(conn: Tx, talk_id: str, data: summaries.SummaryEdit) -> summaries.TalkSummaryView:
    summaries.edit(conn, talk_id, data)
    return summaries.view(conn, talk_id)


@api.post("/talks/{talk_id}/summary/review")
def review_summary(
    conn: Tx, talk_id: str, data: summaries.SummaryReview
) -> summaries.TalkSummaryView:
    summaries.review(conn, talk_id, data)
    return summaries.view(conn, talk_id)


@api.post("/conferences/{conference_id}/summaries/review")
def review_all(conn: Tx, conference_id: str, data: summaries.BulkReview) -> dict[str, object]:
    return summaries.bulk_review(conn, conference_id, data)
