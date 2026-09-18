from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import router
from app.config import load_settings
from app.errors import AppError
from app.gemini import GeminiClient
from app.storage import Storage

log = logging.getLogger(__name__)


def configure_logging() -> None:
    """
    Configure application logging once.

    Individual modules use logging.getLogger(__name__), so the logging policy
    stays centralized here.
    """

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(name)s - "
            "%(message)s"
        ),
    )


@asynccontextmanager
async def lifespan(
    app: FastAPI,
) -> AsyncIterator[None]:
    """
    Initialize long-lived application dependencies.

    Storage owns SQLite/Chroma/filesystem persistence.
    GeminiClient owns the lazy external model client.

    DuckDB connections are intentionally NOT global. sql_executor.py creates
    a fresh in-memory connection for each structured analysis request.
    """

    settings = load_settings()

    store = Storage(
        settings
    )

    gemini = GeminiClient(
        settings
    )

    try:
        store.initialize()

        app.state.settings = settings
        app.state.store = store
        app.state.gemini = gemini

        log.info(
            "application_started"
        )

        yield

    finally:
        try:
            store.close()
        except Exception:
            log.exception(
                "storage_shutdown_failed"
            )

        log.info(
            "application_stopped"
        )


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title="Document Analysis API",
        version="2.0.0",
        lifespan=lifespan,
    )

    # The existing Streamlit frontend may run on another local port.
    # Keeping CORS permissive preserves the original development behavior.
    #
    # For an internet-facing deployment, replace "*" with the exact frontend
    # origins you control.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "*"
        ],
        allow_credentials=False,
        allow_methods=[
            "*"
        ],
        allow_headers=[
            "*"
        ],
    )

    app.include_router(
        router
    )

    @app.exception_handler(
        AppError
    )
    async def app_error_handler(
        request: Request,
        exc: AppError,
    ) -> JSONResponse:
        log.warning(
            "application_error "
            "method=%s path=%s type=%s",
            request.method,
            request.url.path,
            exc.__class__.__name__,
        )

        return JSONResponse(
            status_code=(
                exc.status_code
            ),
            content={
                "detail": exc.message
            },
        )

    @app.exception_handler(
        Exception
    )
    async def unexpected_error_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        log.exception(
            "unexpected_application_error "
            "method=%s path=%s",
            request.method,
            request.url.path,
        )

        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    "An unexpected server error occurred."
                )
            },
        )

    return app


app = create_app()
