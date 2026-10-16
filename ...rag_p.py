import asyncio
import hmac
import logging
import json
from time import perf_counter
from contextlib import asynccontextmanager
from uuid import UUID
from typing import Literal
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from openai import APIConnectionError, APITimeoutError, APIStatusError
from config import settings
from context import context_manager as context
from context.reply_text import plain_reply
from database.connection import initialize as initialize_billing
from database.invoice import invoice_path

log = logging.getLogger(__name__)


def failure_detail(exc):
    """Classify failures without exposing SDK bodies, URLs or credentials."""
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        return 'request_timeout', 'The request exceeded a timeout while waiting for the agent or Azure OpenAI.'
    if isinstance(exc, APIConnectionError):
        return 'azure_connection_failed', ('Unable to establish a connection to Azure OpenAI. '
            'Check the endpoint hostname, network, VPN, proxy, firewall and certificate configuration.')
    if isinstance(exc, APIStatusError):
        messages = {
            400: 'Azure rejected the model request. Check deployment capabilities and API version.',
            401: 'Azure rejected authentication. Check the configured API key and resource.',
            403: 'Azure denied access. Check resource permissions and network access rules.',
            404: 'Azure could not find the requested resource. Check the endpoint, deployment and API version.',
            429: 'Azure rate or quota limits were reached. Check deployment capacity.',
        }
        return 'azure_http_error', messages.get(exc.status_code, 'Azure returned a service error.')
    return 'agent_failed', 'The billing agent encountered an internal error. Check the server traceback for this request.'


async def authorize(x_api_key: str | None = Header(default=None)):
    if settings.API_KEY and not hmac.compare_digest((x_api_key or "").encode(), settings.API_KEY.encode()):
        raise HTTPException(401, "Invalid API key.")


def require_admin(key):
    configured = settings.ADMIN_API_KEY
    if len(configured) < 16 or configured == settings.API_KEY:
        raise HTTPException(503, 'Set a distinct BILLING_ADMIN_API_KEY of at least 16 characters and restart the API.')
    if not hmac.compare_digest((key or '').encode(), configured.encode()):
        raise HTTPException(403, 'Admin access denied.')


def check_record(record, admin_key):
    if not record:
        raise HTTPException(404, 'Conversation not found.')
    if record['mode'] == 'admin':
        require_admin(admin_key)


@asynccontextmanager
async def lifespan(app):
    initialize_billing()
    context.initialize()
    app.state.chat_lock = asyncio.Lock()
    if not hasattr(app.state, "engine"):
        from agent.billing_agent import BillingAgent
        app.state.engine = BillingAgent()
    start = getattr(app.state.engine, "start", None)
    close = getattr(app.state.engine, "close", None)
    try:
        if start:
            await start()
        yield
    finally:
        if close:
            await close()


app = FastAPI(title="Billing Agent", version="6.0.0", lifespan=lifespan,
              dependencies=[Depends(authorize)])


class ConversationRequest(BaseModel):
    customer_id: str | None = Field(default=None, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal['customer', 'admin'] = 'customer'


class ChatRequest(BaseModel):
    conversation_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=10000)


@app.get("/health")
async def health():
    error = settings.azure_configuration_error()
    return {"status": "degraded" if error else "ok", "api": "ready",
            "provider": "azure_openai", "model_configured": error is None,
            "chart_streaming": True,
            "connection_verified": False, "configuration_error": error,
            "model": settings.AZURE_OPENAI_MODEL,
            "deployment": settings.AZURE_OPENAI_DEPLOYMENT}


@app.post("/conversations")
async def create_conversation(body: ConversationRequest, x_admin_key: str | None = Header(default=None)):
    if body.mode == 'admin':
        require_admin(x_admin_key)
    return {"conversation_id": context.conversation(customer_id=body.customer_id, mode=body.mode), 'mode': body.mode}


@app.get('/admin/access')
async def admin_access(x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    return {'authorized': True, 'mode': 'admin', 'read_only': True}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, x_admin_key: str | None = Header(default=None)):
    record = context.get_conversation(str(conversation_id))
    check_record(record, x_admin_key)
    messages = context.history(str(conversation_id), include_artifacts=True)
    messages = [{**m, "content": plain_reply(m["content"])} if m["role"] == "assistant" else m for m in messages]
    return {**record, "messages": messages,
            **({'shopping_state': context.shopping_state(str(conversation_id))} if record['mode'] == 'customer' else {})}


@app.post("/chat")
async def chat(body: ChatRequest, x_admin_key: str | None = Header(default=None)):
    return await run_chat(body, x_admin_key)


@app.post('/chat/stream')
async def stream_chat(body: ChatRequest, x_admin_key: str | None = Header(default=None)):
    # Check access before returning a streaming response or starting any work.
    check_record(context.get_conversation(str(body.conversation_id)), x_admin_key)
    async def events():
        stream_started = perf_counter()
        queue = asyncio.Queue()
        async def run():
            try:
                result = await run_chat(body, x_admin_key,
                    chart_callback=lambda charts: queue.put_nowait({'event':'charts','charts':charts}))
                queue.put_nowait({'event':'result','result':result})
            except HTTPException as exc:
                queue.put_nowait({'event':'error','status':exc.status_code,'detail':exc.detail})
            except Exception:
                log.exception('Streaming chat failed')
                queue.put_nowait({'event':'error','status':503,'detail':'The billing request failed. Check the server logs.'})
        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield '\n'
                    continue
                logging.getLogger('uvicorn.error.billing_performance').info(
                    'billing_stream event=%s seconds=%.3f', event['event'], perf_counter()-stream_started)
                yield json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n'
                if event['event'] in ('result','error'):
                    break
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return StreamingResponse(events(), media_type='application/x-ndjson',
        headers={'Cache-Control':'no-cache', 'X-Accel-Buffering':'no'})


async def run_chat(body, x_admin_key, chart_callback=None):
    cid, rid = str(body.conversation_id), str(body.request_id)
    if not body.message.strip():
        raise HTTPException(422, "Message must not be blank.")
    # One worker and one active turn: no mutable agent state is shared across users.
    queued = perf_counter()
    async with app.state.chat_lock:
        logging.getLogger("uvicorn.error.billing_performance").info(
            "billing_api queue_wait_seconds=%.3f", perf_counter() - queued)
        record = context.get_conversation(cid)
        check_record(record, x_admin_key)
        try:
            cached = context.begin(cid, rid, body.message)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if cached:
            return {**cached, "response": plain_reply(cached["response"])}
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
            kwargs = {'mode': 'admin'} if record['mode'] == 'admin' else {}
            if chart_callback is not None and record['mode'] == 'admin':
                kwargs['chart_callback'] = chart_callback
            if record['mode'] == 'customer':
                kwargs.update(shopping_state=context.shopping_state(cid),
                    state_callback=lambda state, owner: context.checkpoint_shopping(cid, rid, state, owner))
            result = await asyncio.wait_for(app.state.engine.chat(body.message, context.history(cid), record["customer_id"], **kwargs),
                                            timeout=settings.CHAT_TIMEOUT)
            result["response"] = plain_reply(result["response"])
            associated = result.pop('associated_customer_id', None)
            if associated and not record['customer_id'] and record['mode'] == 'customer':
                context.associate_customer(cid, associated)
            response = {"conversation_id": cid, "request_id": rid, **result}
            context.finish(cid, rid, response)
            return response
        except asyncio.CancelledError:
            context.fail(cid, rid)
            raise
        except Exception as exc:
            context.fail(cid, rid)
            log.exception("Billing turn failed: %s", rid)
            code, message = failure_detail(exc)
            raise HTTPException(503, {"code": code, "message": message + ' '
                "If this request included a billing change, check its status before repeating it.",
                "conversation_id": cid, "request_id": rid}) from exc


@app.get("/invoices/{invoice_number}")
async def invoice(invoice_number: str, conversation_id: UUID, customer_id: str | None = None,
                  x_admin_key: str | None = Header(default=None)):
    record = context.get_conversation(str(conversation_id))
    check_record(record, x_admin_key)
    associated = record["customer_id"]
    if associated and customer_id and associated != customer_id:
        raise HTTPException(403, "Invoice customer does not match this conversation.")
    owner = associated or (customer_id if record['mode'] == 'admin' else None)
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
