import os
import sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import streamlit as st
from config import settings

st.set_page_config(
    page_title="Billing Agent",
    page_icon="💳",
    layout="centered",
    initial_sidebar_state="collapsed",
)
API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.API_KEY}


def request(method, route, **kwargs):
    with httpx.Client(timeout=settings.CHAT_TIMEOUT + 30, headers=HEADERS) as client:
        result = client.request(method, API + route, **kwargs)
        result.raise_for_status()
        return result


for key, value in {"messages": [], "conversation_id": None, "pending": None}.items():
    if key not in st.session_state:
        st.session_state[key] = value

with st.sidebar:
    st.subheader("Billing workspace")
    st.caption("Local operator console")
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
        health = request("GET", "/health").json()
        if health["model_configured"]:
            st.success("Azure OpenAI configured")
            st.caption("Connection is checked when you send a message.")
        else:
            st.warning(health.get("configuration_error") or "Configure Azure OpenAI in .env to enable chat.")
    except httpx.HTTPError:
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
        st.markdown(entry["content"])
        show_invoices(entry, i)

prompt = st.chat_input("Ask a billing question...", disabled=bool(st.session_state.pending))
if prompt:
    try:
        if not st.session_state.conversation_id:
            st.session_state.conversation_id = request("POST", "/conversations", json={"customer_id": customer.strip() or None}).json()["conversation_id"]
        st.session_state.pending = {"message": prompt, "request_id": str(uuid4()), "conversation_id": st.session_state.conversation_id}
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()
    except httpx.HTTPError as exc:
        st.error(f"Could not start the conversation: {exc}")

if st.session_state.pending:
    try:
        with st.chat_message("assistant"):
            with st.spinner("Billing Agent is thinking..."):
                reply = request("POST", "/chat", json=st.session_state.pending).json()
        st.session_state.messages.append({"role": "assistant", "content": reply["response"], "invoices": reply.get("invoices", [])})
        st.session_state.pending = None
        st.rerun()
    except httpx.HTTPError as exc:
        detail = exc.response.text if isinstance(exc, httpx.HTTPStatusError) else str(exc)
        st.error(f"Request failed: {detail}")
        st.caption("Check current order state before repeating a write. A timed-out request may have completed.")
        if st.button("Retry same request"):
            st.rerun()
        if st.button("Clear pending request"):
            st.session_state.pending = None
            st.rerun()
