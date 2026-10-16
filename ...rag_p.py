import os
import json
from time import monotonic
import sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import streamlit as st
from config import settings
from context.reply_text import plain_reply, error_reply

st.set_page_config(
    page_title="Billing Agent",
    page_icon="💳",
    layout="centered",
    initial_sidebar_state="collapsed",
)
API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.API_KEY}


@st.cache_resource
def api_client():
    return httpx.Client(timeout=settings.CHAT_TIMEOUT + 30)


def request_headers():
    headers = dict(HEADERS)
    if st.session_state.get('mode') == 'Admin' and st.session_state.get('admin_key'):
        headers['X-Admin-Key'] = st.session_state.admin_key
    return headers


def request(method, route, **kwargs):
    result = api_client().request(method, API + route, headers=request_headers(), **kwargs)
    result.raise_for_status()
    return result


def health_status(api, api_key):
    cached=st.session_state.get('_health_cache')
    if cached and cached[:2]==(api,api_key) and monotonic()-cached[2]<15:
        return cached[3]
    result = api_client().get(api + '/health', headers={'X-API-Key':api_key})
    result.raise_for_status()
    data=result.json()
    st.session_state['_health_cache']=(api,api_key,monotonic(),data)
    return data


def streamed_chat(body, placeholder):
    with api_client().stream('POST', API+'/chat/stream', headers=request_headers(), json=body) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.strip():
                continue
            try:
                event=json.loads(line)
            except ValueError as exc:
                raise httpx.ReadError('Invalid chat stream.') from exc
            if event.get('event')=='charts':
                from ui.charts import render_chart
                with placeholder.container():
                    for chart in event.get('charts',[]):
                        render_chart(chart)
            elif event.get('event')=='result':
                return event['result']
            elif event.get('event')=='error':
                failed=httpx.Response(event.get('status',503),json={'detail':event.get('detail')},
                    request=httpx.Request('POST',API+'/chat/stream'))
                failed.raise_for_status()
    raise httpx.ReadError('Chat stream ended before the saved response arrived.')


for key, value in {"messages": [], "conversation_id": None, "pending": None, 'admin_key': '', 'mode': 'Customer'}.items():
    if key not in st.session_state:
        st.session_state[key] = value


def reset_mode():
    st.session_state.messages = []
    st.session_state.conversation_id = None
    st.session_state.pending = None
    st.session_state.admin_key = ''
    st.session_state.admin_password = ''


def logout():
    reset_mode()
    st.session_state.mode = 'Customer'

with st.sidebar:
    st.subheader("Store assistant")
    st.radio('Mode', ['Customer', 'Admin'], key='mode', on_change=reset_mode)
    if st.session_state.mode == 'Admin':
        st.caption('Read-only analytics and records')
        if not st.session_state.admin_key:
            password = st.text_input('Admin key', type='password', key='admin_password')
            if st.button('Unlock admin mode'):
                try:
                    response = api_client().request('GET', API + '/admin/access',
                        headers={**HEADERS, 'X-Admin-Key': password})
                    response.raise_for_status()
                    st.session_state.admin_key = password
                    st.rerun()
                except httpx.HTTPError:
                    st.error('Admin access denied or not configured. Check BILLING_ADMIN_API_KEY on the API server.')
            st.info('Enter the separate admin key to view store-wide analytics and records.')
            st.stop()
        st.button('Log out of admin mode', on_click=logout)
    else:
        st.caption('Shopping and billing help')
    customer = st.text_input("Customer ID (optional)", placeholder="CUST001", disabled=bool(st.session_state.conversation_id))
    if st.button("New conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation_id = None
        st.session_state.pending = None
        st.rerun()
    resume = st.text_input("Resume conversation ID")
    if st.button("Resume", disabled=not resume):
        try:
            saved = request("GET", f"/conversations/{resume}").json()
            if saved.get('mode', 'customer') != st.session_state.mode.lower():
                st.error('Switch to the matching mode before resuming this conversation.')
                st.stop()
            st.session_state.conversation_id = saved["conversation_id"]
            st.session_state.messages = saved["messages"]
            st.session_state.pending = None
            st.rerun()
        except httpx.HTTPError as exc:
            st.error(f"Could not resume: {exc}")
    if st.session_state.conversation_id:
        st.caption("Conversation ID")
        st.code(st.session_state.conversation_id, language=None)
    try:
        health = health_status(API, settings.API_KEY)
        st.session_state.chart_streaming = bool(health.get('chart_streaming'))
        if health["model_configured"]:
            st.success("Azure OpenAI configured")
            st.caption("Connection is checked when you send a message.")
        else:
            st.warning(health.get("configuration_error") or "Configure Azure OpenAI in .env to enable chat.")
    except httpx.HTTPError:
        st.session_state.chart_streaming = False
        st.warning("Start the API with python app.py")
    st.divider()
    st.caption("Refunds update the ledger only. This console does not send payments.")

st.title("💳 Billing Agent")
st.caption(
    "Ask questions about invoices, orders, refunds, "
    "billing, spending, and costs."
)


def show_invoices(entry, index):
    for n, invoice in enumerate(entry.get("invoices", [])):
        if st.button(f"Prepare PDF · {invoice['invoice_number']}", key=f"pdf-{index}-{n}"):
            try:
                pdf = request("GET", f"/invoices/{invoice['invoice_number']}", params={
                    "conversation_id": st.session_state.conversation_id, "customer_id": invoice["customer_id"]})
                st.download_button("Download invoice", pdf.content, "invoice.pdf", "application/pdf", key=f"dl-{index}-{n}")
            except httpx.HTTPError as exc:
                st.error(f"Invoice unavailable: {exc}")


for i, entry in enumerate(st.session_state.messages):
    with st.chat_message(entry["role"]):
        if entry["role"] == "assistant":
            st.text(plain_reply(entry["content"]))
        else:
            st.markdown(entry["content"])
        if st.session_state.mode == 'Admin' and entry.get('charts'):
            from ui.charts import render_chart
            for chart in entry.get('charts', []):
                render_chart(chart)
        show_invoices(entry, i)

prompt = st.chat_input("Ask a billing question...", disabled=bool(st.session_state.pending))
if prompt:
    try:
        if not st.session_state.conversation_id:
            st.session_state.conversation_id = request("POST", "/conversations", json={
                "customer_id": customer.strip() or None, 'mode': st.session_state.mode.lower()}).json()["conversation_id"]
        st.session_state.pending = {"message": prompt, "request_id": str(uuid4()), "conversation_id": st.session_state.conversation_id}
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()
    except httpx.HTTPError as exc:
        st.error(f"Could not start the conversation: {exc}")

if st.session_state.pending:
    try:
        with st.chat_message("assistant"):
            with st.spinner("Billing Agent is thinking..."):
                if st.session_state.mode == 'Admin' and st.session_state.get('chart_streaming'):
                    reply = streamed_chat(st.session_state.pending, st.empty())
                else:
                    reply = request("POST", "/chat", json=st.session_state.pending).json()
        st.session_state.messages.append({"role": "assistant", "content": reply["response"],
            "invoices": reply.get("invoices", []), 'charts':reply.get('charts',[])})
        st.session_state.pending = None
        st.rerun()
    except httpx.HTTPError as exc:
        detail = error_reply(exc.response) if isinstance(exc, httpx.HTTPStatusError) else "Unable to reach the Billing Agent. Check that the API is running."
        st.error(f"Request failed: {detail}")
        st.caption("Check current order state before repeating a write. A timed-out request may have completed.")
        if st.button("Retry same request"):
            st.rerun()
        if st.button("Clear pending request"):
            st.session_state.pending = None
            st.rerun()
