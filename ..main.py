from __future__ import annotations
import truststore

truststore.inject_into_ssl()
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api import router
from app.config import Settings, load_settings, setup_logging
from app.errors import AppError, http_error
from app.gemini import GeminiClient
from app.storage import Storage

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    gemini: object | None = None,
) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging()
        store = Storage(settings)
        store.init()
        app.state.settings = settings
        app.state.store = store
        app.state.gemini = gemini or GeminiClient(settings)
        app.state.gemini_is_stub = gemini is not None
        log.info("application_started model=%s", settings.gemini_model)
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="Intelligent Document Analysis System", lifespan=lifespan)
    app.state.settings = settings

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        http_exc = http_error(exc)
        return JSONResponse(status_code=http_exc.status_code, content={"detail": http_exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        log.info("api_validation_error")
        return JSONResponse(
            status_code=422,
            content={"detail": {"error": "validation_error", "message": "The request is invalid."}},
        )

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        log.exception("unhandled_error")
        return JSONResponse(
            status_code=500,
            content={"detail": {"error": "internal_error", "message": "An unexpected error occurred."}},
        )

    app.include_router(router)
    return app


app = create_app()


