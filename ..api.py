from __future__ import annotations

import asyncio

from fastapi import APIRouter, File, Request, UploadFile

from app.chat import ask, get_conversation
from app.errors import AppError, FileValidationError, http_error
from app.ingest import ingest_bytes, to_document_out
from app.models import ChatRequest, ChatResponse, ConversationOut, DocumentOut, HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
      store = request.app.state.store
      sqlite_ok = "ok"
      chroma_ok = "ok"
      try:
            store.conn.execute("SELECT 1").fetchone()
      except Exception:
            sqlite_ok = "error"
      if store.collection is None:
            chroma_ok = "error"
      return HealthResponse(
            status="ok" if sqlite_ok == "ok" and chroma_ok == "ok" else "degraded",
            sqlite=sqlite_ok,
            chroma=chroma_ok,
            gemini_configured=bool(request.app.state.settings.gemini_api_key)
            or bool(getattr(request.app.state, "gemini_is_stub", False)),
      )


@router.post("/documents/upload", response_model=DocumentOut, status_code=201)
async def upload_document(request: Request, file: UploadFile = File(...)) -> DocumentOut:
      max_bytes = request.app.state.settings.max_upload_bytes
      if file.size is not None and file.size > max_bytes:
            raise http_error(FileValidationError("The uploaded file exceeds the size limit."))
      data = await file.read()
      filename = file.filename or "upload"
      try:
            return await asyncio.to_thread(
                  ingest_bytes,
                  filename=filename,
                  data=data,
                  store=request.app.state.store,
                  gemini=request.app.state.gemini,
                  settings=request.app.state.settings,
            )
      except AppError as exc:
            raise http_error(exc) from exc


@router.get("/documents", response_model=list[DocumentOut])
def list_documents(request: Request) -> list[DocumentOut]:
      store = request.app.state.store
      return [to_document_out(store, doc) for doc in store.list_documents()]


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, request: Request) -> dict[str, str]:
      try:
            request.app.state.store.delete_document(document_id)
      except AppError as exc:
            raise http_error(exc) from exc
      return {"status": "deleted", "document_id": document_id}


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, request: Request) -> ChatResponse:
      try:
            return ask(
                  question=body.question.strip(),
                  conversation_id=body.conversation_id,
                  store=request.app.state.store,
                  gemini=request.app.state.gemini,
                  settings=request.app.state.settings,
            )
      except AppError as exc:
            raise http_error(exc) from exc


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def read_conversation(conversation_id: str, request: Request) -> ConversationOut:
      try:
            return get_conversation(request.app.state.store, conversation_id)
      except AppError as exc:
            raise http_error(exc) from exc


@router.delete("/conversations/{conversation_id}")
def remove_conversation(conversation_id: str, request: Request) -> dict[str, str]:
      try:
            request.app.state.store.delete_conversation(conversation_id)
      except AppError as exc:
            raise http_error(exc) from exc
      return {"status": "deleted", "conversation_id": conversation_id}

