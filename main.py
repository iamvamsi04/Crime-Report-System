"""FastAPI REST API for local document analysis."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status

from app.core.config import settings
from app.core.exceptions import (
    DocumentAnalysisError,
    EvidenceUnavailableError,
    FileValidationError,
    LLMConfigurationError,
    PlanningError,
    RuntimeConfigurationError,
)
from app.core.logging import configure_logging
from app.core.models import AnswerResponse, DocumentRecord, QuestionRequest, UploadResult
from app.services.container import ApplicationServices, build_services, get_services


configure_logging(settings.log_level)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def application_lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Initialize required local stores before traffic, keeping a clear degraded status on failure."""
    application.state.runtime_error = None
    application.state.services = None
    try:
        application.state.services = build_services(settings)
        logger.info("Local runtime preflight passed; ChromaDB collections are ready.")
    except RuntimeConfigurationError as exc:
        application.state.runtime_error = str(exc)
        logger.critical("Application startup configuration error: %s", exc)
    except DocumentAnalysisError as exc:
        application.state.runtime_error = f"Local runtime initialization failed: {exc}"
        logger.critical("Application startup initialization error: %s", exc)
    yield


app = FastAPI(
    title="Intelligent Document Analysis System",
    version="1.0.0",
    description="Local PDF/TXT/CSV retrieval and analysis with Gemini used only for planning and grounded wording.",
    lifespan=application_lifespan,
)
app.state.runtime_error = None
app.state.services = None


def service_dependency(request: Request) -> ApplicationServices:
    """Return initialized services or a deliberate 503 instead of a lazy, unhandled failure."""
    runtime_error = getattr(request.app.state, "runtime_error", None)
    if runtime_error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=runtime_error)
    initialized_services = getattr(request.app.state, "services", None)
    if initialized_services is not None:
        return initialized_services
    # Supports direct programmatic use outside Uvicorn's lifespan while preserving the same checks.
    try:
        return get_services()
    except RuntimeConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@app.get("/health")
def health(request: Request) -> dict[str, object]:
    """Return non-sensitive local configuration information."""
    runtime_error = getattr(request.app.state, "runtime_error", None)
    return {
        "status": "degraded" if runtime_error else "ok",
        "gemini_configured": bool(settings.gemini_api_key),
        "embedding_model": settings.embedding_model,
        "chroma_distance_space": settings.chroma_distance_space,
        "local_retrieval_ready": not bool(runtime_error),
        "configuration_error": runtime_error,
    }


@app.get("/documents", response_model=list[DocumentRecord])
def list_documents(services: ApplicationServices = Depends(service_dependency)) -> list[DocumentRecord]:
    return services.catalog.list()


@app.post("/documents/load-local", response_model=UploadResult)
def load_local_documents(services: ApplicationServices = Depends(service_dependency)) -> UploadResult:
    """Load supported top-level files from the configured local documents directory."""
    return UploadResult(documents=services.document_service.ingest_directory(settings.documents_dir))


@app.post("/documents/upload", response_model=UploadResult, status_code=status.HTTP_201_CREATED)
async def upload_documents(
    files: list[UploadFile] = File(...), services: ApplicationServices = Depends(service_dependency)
) -> UploadResult:
    """Save uploaded files locally, then run normal validation and ingestion."""
    settings.ensure_directories()
    records: list[DocumentRecord] = []
    for uploaded_file in files:
        original_name = Path(uploaded_file.filename or "upload").name
        destination = settings.upload_dir / f"{uuid.uuid4()}_{original_name}"
        try:
            content = await uploaded_file.read()
            destination.write_bytes(content)
            records.append(services.document_service.ingest_path(destination))
        except FileValidationError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        finally:
            await uploaded_file.close()
    return UploadResult(documents=records)


@app.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(services: ApplicationServices = Depends(service_dependency)) -> dict[str, str]:
    return {"session_id": services.context_manager.get_or_create_session(None)}


@app.get("/sessions/{session_id}/history")
def session_history(session_id: str, services: ApplicationServices = Depends(service_dependency)) -> list[dict[str, object]]:
    try:
        return [turn.model_dump(mode="json") for turn in services.context_manager.history(session_id)]
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.post("/questions", response_model=AnswerResponse)
def ask_question(
    request: QuestionRequest, services: ApplicationServices = Depends(service_dependency)
) -> AnswerResponse:
    try:
        return services.question_service.ask(request.question, request.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (FileValidationError, PlanningError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except (LLMConfigurationError, EvidenceUnavailableError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except DocumentAnalysisError as exc:
        logger.warning("Controlled analysis error: %s", exc)
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
