"""Request-scoped database access and application resources."""

import sqlite3
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request

from rapporteur import (
    db,
)
from rapporteur.processing import Processor
from rapporteur.storage import Storage


def _database(request: Request) -> db.Database:
    database: db.Database = request.app.state.database
    return database


def _tx(request: Request) -> Iterator[sqlite3.Connection]:
    with _database(request).tx() as conn:
        yield conn


def _storage(request: Request) -> Storage:
    storage: Storage = request.app.state.storage
    return storage


def _wake(request: Request) -> None:
    processor: Processor = request.app.state.processor
    processor.wake()


Tx = Annotated[sqlite3.Connection, Depends(_tx, scope="function")]


def _read(request: Request) -> Iterator[sqlite3.Connection]:
    with _database(request).read() as conn:
        yield conn


ReadTx = Annotated[sqlite3.Connection, Depends(_read)]
