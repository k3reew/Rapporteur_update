"""HTTP endpoints for talks."""

import json

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from rapporteur import (
    db,
    materials,
)
from rapporteur.api.dependencies import ReadTx, Tx
from rapporteur.contracts import Speaker
from rapporteur.contracts.base import NonEmptyStr
from rapporteur.errors import NotFoundError, UserError

api = APIRouter()


class TalkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: NonEmptyStr
    section_id: str | None = None


class TalkUpdate(BaseModel):
    """Карточка доклада. organizations и contacts без привязки к автору: None оставляет
    прежние значения, чтобы правка темы и авторов их не стирала."""

    model_config = ConfigDict(extra="forbid")

    title: NonEmptyStr | None
    speakers: list[Speaker] = Field(default_factory=list)
    organizations: list[NonEmptyStr] | None = None
    contacts: list[NonEmptyStr] | None = None
    revision: int | None = None


@api.post("/conferences/{conference_id}/talks", status_code=201)
def create_talk(conn: Tx, conference_id: str, data: TalkCreate) -> materials.Materials:
    materials.add_talk(conn, conference_id, data.title, data.section_id)
    return materials.review(conn, conference_id)


@api.put("/talks/{talk_id}", status_code=204)
def update_talk(conn: Tx, talk_id: str, data: TalkUpdate) -> Response:
    """Карточка реестра: тема, авторы, организации и контакты идут в справку вместо ответа
    модели (ADR-008)."""
    current = db.one(conn, "SELECT * FROM talks WHERE id=?", talk_id)
    if current is None:
        raise NotFoundError("Доклад не найден")
    if data.revision is not None and data.revision != current["input_revision"]:
        raise UserError("Карточка доклада изменилась: обновите страницу", 409)
    organizations = (
        json.loads(current["organizations"]) if data.organizations is None else data.organizations
    )
    contacts = json.loads(current["contacts"]) if data.contacts is None else data.contacts
    updated = conn.execute(
        "UPDATE talks SET title = ?, speakers = ?, organizations = ?, contacts = ?, "
        "input_revision=input_revision+1 WHERE id = ?",
        (
            data.title,
            db.dumps([speaker.model_dump(mode="json") for speaker in data.speakers]),
            db.dumps(organizations),
            db.dumps(contacts),
            talk_id,
        ),
    ).rowcount
    if not updated:
        raise NotFoundError("Доклад не найден")
    return Response(status_code=204)


@api.get("/talks/{talk_id}")
def talk_metadata(conn: ReadTx, talk_id: str) -> dict[str, object]:
    row = db.one(conn, "SELECT * FROM talks WHERE id=?", talk_id)
    if row is None:
        raise NotFoundError("Доклад не найден")
    return {
        "title": row["title"],
        "speakers": json.loads(row["speakers"]),
        "organizations": json.loads(row["organizations"]),
        "contacts": json.loads(row["contacts"]),
        "revision": row["input_revision"],
    }
