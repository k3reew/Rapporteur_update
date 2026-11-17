"""Application factory, resource lifecycle, errors and frontend hosting."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from rapporteur import (
    db,
    settings,
)
from rapporteur.api import api
from rapporteur.errors import UserError
from rapporteur.llm.client import make_client
from rapporteur.processing import ClientFactory, Processor
from rapporteur.storage import Storage

log = logging.getLogger("rapporteur")


def create_app(
    data_dir: Path | None = None,
    static_dir: Path | None = None,
    client: ClientFactory | None = None,
    *,
    process: bool = True,
) -> FastAPI:
    """process=False — без фонового потока: тесты вызывают processor.run_once() сами."""
    # uvicorn настраивает только свои логгеры; журнал обработки пишется рядом с ними.
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    data_dir = data_dir or settings.DATA_DIR
    database = db.Database(data_dir / "rapporteur.db")
    storage = Storage(data_dir)
    processor = Processor(
        database,
        storage,
        client or (lambda profile: make_client(profile, settings.OLLAMA_URL)),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if process:
            processor.start()
        try:
            yield
        finally:
            processor.stop()

    app = FastAPI(
        title="Репортёр",
        version=version("rapporteur"),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.database = database
    app.state.storage = storage
    app.state.processor = processor

    @app.exception_handler(UserError)
    async def user_error(_request: Request, error: UserError) -> JSONResponse:
        return JSONResponse({"message": error.message}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, error: RequestValidationError) -> JSONResponse:
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = f"Некорректный запрос: {where} — {first.get('msg', 'ошибка')}".strip()
        return JSONResponse({"message": message}, status_code=422)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": app.version}

    app.include_router(api)

    # Статика после API: /health и /api/v1 имеют приоритет.
    if static_dir is None and settings.STATIC_DIR:
        static_dir = Path(settings.STATIC_DIR)
    if static_dir is not None:
        if not (static_dir / "index.html").is_file():
            raise RuntimeError(f"Не найдена сборка фронтенда: {static_dir / 'index.html'}")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    return app
