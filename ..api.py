from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

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


@router.get("/health")
async def health(request: Request) -> dict[str, str]:
    settings = request.app.state.settings

    return {
        "status": "ok",
        "service": "document-analysis",
        "model": settings.gemini_model,
    }


@router.get(
    "/documents",
    response_model=list[DocumentOut],
)
async def list_documents(request: Request) -> list[DocumentOut]:
    store = request.app.state.store

    documents = store.list_documents(
        ready_only=False,
    )

    return [
        DocumentOut.model_validate(
            document,
            from_attributes=False,
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
    settings = request.app.state.settings
    store = request.app.state.store
    gemini = request.app.state.gemini

    filename = Path(file.filename or "").name

    if not filename:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "validation_error",
                "message": "A filename is required.",
            },
        )

    suffix = Path(filename).suffix.lower()

    allowed = {
        ".pdf",
        ".txt",
        ".csv",
        ".xlsx",
        ".xls",
    }

    if suffix not in allowed:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_file_type",
                "message": (
                    "Only PDF, TXT, CSV, XLSX, and XLS files "
                    "are supported."
                ),
            },
        )

    settings.upload_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path: Path | None = None

    try:
        with NamedTemporaryFile(
            mode="wb",
            suffix=suffix,
            dir=settings.upload_dir,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

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
                                "The uploaded file exceeds the "
                                "configured maximum size."
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
            document=DocumentOut.model_validate(
                document,
                from_attributes=False,
            )
        )

    finally:
        await file.close()

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


@router.delete(
    "/documents/{document_id}",
)
async def delete_document(
    document_id: str,
    request: Request,
) -> dict[str, str]:
    store = request.app.state.store

    try:
        store.delete_document(document_id)
    except NotFoundError:
        raise

    return {
        "status": "deleted",
        "document_id": document_id,
    }


@router.post(
    "/chat",
    response_model=ChatResponse,
)
async def chat(
    payload: ChatRequest,
    request: Request,
) -> ChatResponse:
    settings = request.app.state.settings
    store = request.app.state.store
    gemini = request.app.state.gemini

    return await ask(
        question=payload.question,
        conversation_id=payload.conversation_id,
        store=store,
        gemini=gemini,
        settings=settings,
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationOut,
)
async def conversation(
    conversation_id: str,
    request: Request,
) -> ConversationOut:
    store = request.app.state.store

    return get_conversation(
        store=store,
        conversation_id=conversation_id,
    )
