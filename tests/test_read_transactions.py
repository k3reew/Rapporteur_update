"""Read-only HTTP requests remain available while a worker reserves a write."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rapporteur import db
from rapporteur.app import create_app


def test_conference_list_can_read_during_worker_write_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path, process=False)
    database: db.Database = app.state.database
    original = database.connect

    def connect() -> sqlite3.Connection:
        conn = original()
        conn.execute("PRAGMA busy_timeout=10")
        return conn

    monkeypatch.setattr(database, "connect", connect)
    with TestClient(app) as client, database.tx():
        response = client.get("/api/v1/conferences")
        assert response.status_code == 200
        assert response.json() == []


def test_read_connection_rejects_writes(tmp_path: Path) -> None:
    database = db.Database(tmp_path / "test.db")
    with database.read() as conn:
        assert conn.execute("SELECT count(*) FROM conferences").fetchone()[0] == 0
        try:
            conn.execute("DELETE FROM conferences")
        except sqlite3.OperationalError as error:
            assert "readonly" in str(error).lower()
        else:
            raise AssertionError("Read connection accepted a write")


def test_mutations_commit_before_http_response_starts(tmp_path: Path) -> None:
    """A follow-up GET must see a mutation as soon as its response is received."""
    from starlette.types import Message, Receive, Scope, Send

    app = create_app(tmp_path, process=False)
    database: db.Database = app.state.database
    observed = []

    async def inspect_response(scope: Scope, receive: Receive, send: Send) -> None:
        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                with database.read() as conn:
                    observed.append(conn.execute("SELECT count(*) FROM conferences").fetchone()[0])
            await send(message)

        await app(scope, receive, capture)

    with TestClient(inspect_response) as client:
        response = client.post("/api/v1/conferences", json={"title": "Проверка"})
        assert response.status_code == 201
        assert observed == [1]
