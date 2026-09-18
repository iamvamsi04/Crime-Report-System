from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Request,
    UploadFile,
)

from app.chat import ask
from app.errors import AppError, NotFoundError, http_error
from app.ingest import ingest_bytes
from app.models import (
    ChatRequest,
    ChatResponse,
    ConversationMessage,
    ConversationResponse,
    DeleteResponse,
    DocumentResponse,
    UploadResponse,
)
from app.storage import Storage

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get(
    "/health",
)
async def health() -> dict[str, str]:
    return {
        "status": "ok",
    }


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


@router.post(
    "/documents/upload",
    response_model=UploadResponse,
)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
) -> UploadResponse:
    store = _store(
        request
    )

    gemini = _gemini(
        request
    )

    settings = _settings(
        request
    )

    filename = (
        file.filename
        or ""
    ).strip()

    try:
        data = await file.read()

        document = await asyncio.to_thread(
            ingest_bytes,
            filename=filename,
            data=data,
            store=store,
            gemini=gemini,
            settings=settings,
        )

        return UploadResponse(
            document=_document_response(
                document
            )
        )

    except AppError as exc:
        raise http_error(
            exc
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        log.exception(
            "document_upload_failed filename=%s",
            filename,
        )

        raise http_error(
            exc
        ) from exc

    finally:
        await file.close()


@router.get(
    "/documents",
    response_model=list[
        DocumentResponse
    ],
)
async def list_documents(
    request: Request,
) -> list[DocumentResponse]:
    store = _store(
        request
    )

    try:
        documents = await asyncio.to_thread(
            store.list_documents
        )

        return [
            _document_response(
                document
            )
            for document in documents
        ]

    except AppError as exc:
        raise http_error(
            exc
        ) from exc

    except Exception as exc:
        log.exception(
            "document_list_failed"
        )

        raise http_error(
            exc
        ) from exc


@router.delete(
    "/documents/{document_id}",
    response_model=DeleteResponse,
)
async def delete_document(
    document_id: str,
    request: Request,
) -> DeleteResponse:
    store = _store(
        request
    )

    try:
        await asyncio.to_thread(
            store.delete_document,
            document_id,
        )

        return DeleteResponse(
            deleted=True,
            document_id=document_id,
        )

    except AppError as exc:
        raise http_error(
            exc
        ) from exc

    except Exception as exc:
        log.exception(
            "document_delete_failed document_id=%s",
            document_id,
        )

        raise http_error(
            exc
        ) from exc


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@router.post(
    "/chat",
    response_model=ChatResponse,
)
async def chat(
    payload: ChatRequest,
    request: Request,
) -> ChatResponse:
    """
    Chat is intentionally run in a worker thread because the current Gemini,
    SQLite, Chroma, Pandas, and DuckDB interfaces used by the application are
    synchronous.

    This keeps FastAPI's event loop responsive without forcing artificial
    async wrappers throughout the analysis stack.
    """

    store = _store(
        request
    )

    gemini = _gemini(
        request
    )

    question = (
        payload.question
        or ""
    ).strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail=(
                "A question is required."
            ),
        )

    try:
        return await asyncio.to_thread(
            ask,
            question,
            payload.conversation_id,
            store,
            gemini,
        )

    except AppError as exc:
        raise http_error(
            exc
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        log.exception(
            "chat_failed conversation_id=%s",
            payload.conversation_id,
        )

        raise http_error(
            exc
        ) from exc


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationResponse,
)
async def get_conversation(
    conversation_id: str,
    request: Request,
) -> ConversationResponse:
    store = _store(
        request
    )

    try:
        conversation = await asyncio.to_thread(
            store.get_conversation,
            conversation_id,
        )

        if conversation is None:
            raise NotFoundError(
                "Conversation not found."
            )

        messages = await asyncio.to_thread(
            store.list_messages,
            conversation_id,
        )

        return ConversationResponse(
            conversation_id=conversation_id,
            messages=[
                _conversation_message(
                    message
                )
                for message in messages
            ],
        )

    except AppError as exc:
        raise http_error(
            exc
        ) from exc

    except Exception as exc:
        log.exception(
            "conversation_read_failed conversation_id=%s",
            conversation_id,
        )

        raise http_error(
            exc
        ) from exc


@router.delete(
    "/conversations/{conversation_id}",
    response_model=DeleteResponse,
)
async def delete_conversation(
    conversation_id: str,
    request: Request,
) -> DeleteResponse:
    store = _store(
        request
    )

    try:
        await asyncio.to_thread(
            store.delete_conversation,
            conversation_id,
        )

        return DeleteResponse(
            deleted=True,
            conversation_id=conversation_id,
        )

    except AppError as exc:
        raise http_error(
            exc
        ) from exc

    except Exception as exc:
        log.exception(
            "conversation_delete_failed conversation_id=%s",
            conversation_id,
        )

        raise http_error(
            exc
        ) from exc


# ---------------------------------------------------------------------------
# Application dependencies
# ---------------------------------------------------------------------------


def _store(
    request: Request,
) -> Storage:
    store = getattr(
        request.app.state,
        "store",
        None,
    )

    if store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Storage is not initialized."
            ),
        )

    return store


def _gemini(
    request: Request,
) -> Any:
    gemini = getattr(
        request.app.state,
        "gemini",
        None,
    )

    if gemini is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Gemini is not initialized."
            ),
        )

    return gemini


def _settings(
    request: Request,
) -> Any:
    settings = getattr(
        request.app.state,
        "settings",
        None,
    )

    if settings is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Application settings are not initialized."
            ),
        )

    return settings


# ---------------------------------------------------------------------------
# Response conversion
# ---------------------------------------------------------------------------


def _document_response(
    document: dict[str, Any],
) -> DocumentResponse:
    """
    Keep internal persistence fields such as stored_name/file_hash out of the
    public response while exposing the metadata used by the current frontend.
    """

    profile = _parse_json(
        document.get(
            "csv_profile"
        ),
        default=None,
    )

    return DocumentResponse(
        id=str(
            document["id"]
        ),
        filename=str(
            document["filename"]
        ),
        file_type=str(
            document["file_type"]
        ),
        status=str(
            document["status"]
        ),
        chunk_count=int(
            document.get(
                "chunk_count"
            )
            or 0
        ),
        page_count=_optional_int(
            document.get(
                "page_count"
            )
        ),
        csv_profile=profile,
        error_message=_optional_string(
            document.get(
                "error_message"
            )
        ),
        created_at=_optional_string(
            document.get(
                "created_at"
            )
        ),
    )


def _conversation_message(
    message: dict[str, Any],
) -> ConversationMessage:
    return ConversationMessage(
        role=str(
            message.get(
                "role"
            )
            or ""
        ),
        content=str(
            message.get(
                "content"
            )
            or ""
        ),
        status=_optional_string(
            message.get(
                "status"
            )
        ),
        sources=_parse_json(
            message.get(
                "sources_json"
            ),
            default=[],
        ),
        execution_flow=_parse_json(
            message.get(
                "execution_flow_json"
            ),
            default=[],
        ),
        query_plan=_parse_json(
            message.get(
                "query_plan_json"
            ),
            default=None,
        ),
        created_at=_optional_string(
            message.get(
                "created_at"
            )
        ),
    )


def _parse_json(
    value: Any,
    *,
    default: Any,
) -> Any:
    if value is None:
        return default

    if isinstance(
        value,
        (
            dict,
            list,
        ),
    ):
        return value

    if not isinstance(
        value,
        str,
    ):
        return default

    text = value.strip()

    if not text:
        return default

    try:
        return json.loads(
            text
        )
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return default


def _optional_string(
    value: Any,
) -> str | None:
    if value is None:
        return None

    text = str(
        value
    ).strip()

    return (
        text
        if text
        else None
    )


def _optional_int(
    value: Any,
) -> int | None:
    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    try:
        return int(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return None
