import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from uuid import UUID
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from config import settings
from context import context_manager as context
from database.connection import initialize as initialize_billing
from database.invoice import invoice_path

log = logging.getLogger(__name__)


async def authorize(x_api_key: str | None = Header(default=None)):
    if settings.API_KEY and not hmac.compare_digest(x_api_key or "", settings.API_KEY):
        raise HTTPException(401, "Invalid API key.")


@asynccontextmanager
async def lifespan(app):
    initialize_billing()
    context.initialize()
    app.state.chat_lock = asyncio.Lock()
    if not hasattr(app.state, "engine"):
        from agent.billing_agent import BillingAgent
        app.state.engine = BillingAgent()
    yield


app = FastAPI(title="Billing Agent", version="1.0.0", lifespan=lifespan,
              dependencies=[Depends(authorize)])


class ConversationRequest(BaseModel):
    customer_id: str | None = Field(default=None, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")


class ChatRequest(BaseModel):
    conversation_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=10000)


@app.get("/health")
async def health():
    error = settings.azure_configuration_error()
    return {"status": "degraded" if error else "ok", "api": "ready",
            "provider": "azure_openai", "model_configured": error is None,
            "connection_verified": False, "configuration_error": error,
            "model": settings.AZURE_OPENAI_MODEL,
            "deployment": settings.AZURE_OPENAI_DEPLOYMENT}


@app.post("/conversations")
async def create_conversation(body: ConversationRequest):
    return {"conversation_id": context.conversation(customer_id=body.customer_id)}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID):
    record = context.get_conversation(str(conversation_id))
    if not record:
        raise HTTPException(404, "Conversation not found.")
    return {**record, "messages": context.history(str(conversation_id))}


@app.post("/chat")
async def chat(body: ChatRequest):
    cid, rid = str(body.conversation_id), str(body.request_id)
    if not body.message.strip():
        raise HTTPException(422, "Message must not be blank.")
    # One worker and one active turn: no mutable agent state is shared across users.
    async with app.state.chat_lock:
        record = context.get_conversation(cid)
        if not record:
            raise HTTPException(404, "Conversation not found.")
        try:
            cached = context.begin(cid, rid, body.message)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if cached:
            return cached
        try:
            validate = getattr(app.state.engine, "validate_configuration", None)
            if validate:
                validate()
        except ValueError as exc:
            # No tools have run. Remove this pending request so a configured restart
            # can retry the same request ID safely.
            context.discard_pending(cid, rid)
            raise HTTPException(503, str(exc)) from exc
        try:
            result = await asyncio.wait_for(app.state.engine.chat(body.message, context.history(cid), record["customer_id"]),
                                            timeout=settings.CHAT_TIMEOUT)
            response = {"conversation_id": cid, "request_id": rid, **result}
            context.finish(cid, rid, response)
            return response
        except Exception as exc:
            context.fail(cid, rid)
            log.exception("Billing turn failed: %s", rid)
            raise HTTPException(503, {"message": "Agent unavailable or timed out. Check the Azure OpenAI endpoint, deployment, API version and key. "
                "A billing tool may already have committed; check the order before repeating a write.",
                "conversation_id": cid, "request_id": rid}) from exc


@app.get("/invoices/{invoice_number}")
async def invoice(invoice_number: str, conversation_id: UUID, customer_id: str | None = None):
    record = context.get_conversation(str(conversation_id))
    if not record:
        raise HTTPException(404, "Conversation not found.")
    associated = record["customer_id"]
    if associated and customer_id and associated != customer_id:
        raise HTTPException(403, "Invoice customer does not match this conversation.")
    owner = associated or customer_id
    if not owner:
        raise HTTPException(422, "A customer ID is required.")
    try:
        result = await asyncio.wait_for(app.state.engine.invoice(invoice_number, owner), timeout=60)
    except Exception as exc:
        raise HTTPException(503, "Invoice tool unavailable.") from exc
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Invoice not found."))
    path = invoice_path(invoice_number, owner)
    if not path.is_file():
        raise HTTPException(503, "Invoice file was not generated.")
    return FileResponse(path, media_type="application/pdf", filename="invoice.pdf")
