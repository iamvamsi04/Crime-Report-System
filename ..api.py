from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app.chat import ask, get_conversation
from app.errors import NotFoundError
from app.ingest import ingest_document
from app.models import (
    ChatRequest,
    ChatResponse,
    ConversationOut,
    DocumentOut,
    UploadResponse,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ============================================================
# Health
# ============================================================


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """
    Basic backend health endpoint.

    This does not call Gemini because health checks should remain cheap
    and should not depend on an external API.
    """

    settings = request.app.state.settings

    return {
        "status": "ok",
        "service": "document-analysis",
        "model": settings.gemini_model,
    }


# ============================================================
# Documents
# ============================================================


@router.get(
    "/documents",
    response_model=list[DocumentOut],
)
async def list_documents(
    request: Request,
) -> list[DocumentOut]:
    """
    Return all successfully ingested documents.
    """

    store = request.app.state.store

    documents = store.list_documents(
        ready_only=False,
    )

    return [
        DocumentOut(
            **document,
        )
        for document in documents
    ]


@router.post(
    "/documents/upload",
    response_model=UploadResponse,
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
) -> UploadResponse:
    """
    Upload and ingest one document.

    PDF/TXT:
        extraction → chunking → embeddings → Chroma

    CSV/Excel:
        validation/profile → SQLite metadata

    The ingestion implementation remains in ingest.py.
    """

    settings = request.app.state.settings
    store = request.app.state.store
    gemini = request.app.state.gemini

    if not file.filename:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_file",
                "message": "A filename is required.",
            },
        )

    filename = Path(file.filename).name

    if not filename:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_file",
                "message": "The uploaded filename is invalid.",
            },
        )

    suffix = Path(filename).suffix.lower()

    supported = {
        ".pdf",
        ".txt",
        ".csv",
        ".xlsx",
        ".xls",
    }

    if suffix not in supported:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_file_type",
                "message": (
                    "Supported file types are PDF, TXT, CSV, XLSX, and XLS."
                ),
            },
        )

    upload_dir = settings.upload_dir
    upload_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            dir=upload_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(
                temporary_file.name
            )

            total_bytes = 0

            while True:
                chunk = await file.read(1024 * 1024)

                if not chunk:
                    break

                total_bytes += len(chunk)

                if total_bytes > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "error": "file_too_large",
                            "message": (
                                "The uploaded file exceeds the configured "
                                "maximum size."
                            ),
                        },
                    )

                temporary_file.write(chunk)

        document = ingest_document(
            path=temporary_path,
            original_filename=filename,
            store=store,
            gemini=gemini,
            settings=settings,
        )

        return UploadResponse(
            document=DocumentOut(
                **document,
            )
        )

    except HTTPException:
        raise

    except Exception:
        log.exception(
            "document_upload_failed filename=%s",
            filename,
        )

        raise HTTPException(
            status_code=500,
            detail={
                "error": "ingestion_failed",
                "message": (
                    "The document could not be ingested."
                ),
            },
        )

    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(
                    missing_ok=True,
                )
            except OSError:
                log.warning(
                    "temporary_upload_cleanup_failed path=%s",
                    temporary_path,
                )

        await file.close()


@router.get(
    "/documents/{document_id}",
    response_model=DocumentOut,
)
async def get_document(
    document_id: str,
    request: Request,
) -> DocumentOut:
    """
    Return metadata for one document.
    """

    store = request.app.state.store

    try:
        document = store.get_document(
            document_id,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "document_not_found",
                "message": "The requested document was not found.",
            },
        )

    return DocumentOut(
        **document,
    )


@router.delete(
    "/documents/{document_id}",
)
async def delete_document(
    document_id: str,
    request: Request,
) -> dict[str, Any]:
    """
    Delete a document and all associated ingestion data.
    """

    store = request.app.state.store

    try:
        store.delete_document(
            document_id,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "document_not_found",
                "message": "The requested document was not found.",
            },
        )

    return {
        "status": "deleted",
        "document_id": document_id,
    }


# ============================================================
# Chat
# ============================================================


@router.post(
    "/chat",
    response_model=ChatResponse,
)
async def chat(
    payload: ChatRequest,
    request: Request,
) -> ChatResponse:
    """
    Process a natural-language question.

    The API layer does not decide whether the question needs:
        - RAG
        - CSV/Excel analysis
        - both

    That decision belongs to chat.py / plan.py.
    """

    store = request.app.state.store
    gemini = request.app.state.gemini
    settings = request.app.state.settings

    return ask(
        question=payload.question,
        conversation_id=payload.conversation_id,
        store=store,
        gemini=gemini,
        settings=settings,
    )


# ============================================================
# Conversations
# ============================================================


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
)
async def conversation(
    conversation_id: str,
    request: Request,
) -> ConversationOut:
    """
    Return the complete conversation history.
    """

    store = request.app.state.store

    try:
        return get_conversation(
            store=store,
            conversation_id=conversation_id,
        )
    except NotFoundError:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "conversation_not_found",
                "message": "The requested conversation was not found.",
            },
        )
