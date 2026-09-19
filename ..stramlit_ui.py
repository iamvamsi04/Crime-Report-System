from __future__ import annotations

import httpx
import streamlit as st

st.set_page_config(page_title="Document Analysis", layout="wide")

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None
if "messages" not in st.session_state:
    st.session_state.messages = []
if "pending_question" not in st.session_state:
    st.session_state.pending_question = None

LIST_TIMEOUT = 5.0
UPLOAD_TIMEOUT = 300.0
CHAT_TIMEOUT = 180.0

def api_url(base: str, path: str) -> str:
    return base.rstrip("/") + path

def request_error_message(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "Cannot reach the FastAPI backend. Start Uvicorn on the Backend URL (port 8000)."
    if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.TimeoutException)):
        return "The backend is still working or took too long. Keep Uvicorn running and try again."
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("detail")
            if isinstance(detail, dict) and detail.get("message"):
                return str(detail["message"])
        except Exception:
            return "The backend returned an error."
        return "The backend returned an error."
    return "The request failed."

def fetch_documents(base: str) -> tuple[list[dict], str | None]:
    try:
        response = httpx.get(api_url(base, "/documents"), timeout=LIST_TIMEOUT)
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return [], request_error_message(exc)

st.title("Intelligent Document Analysis")
st.caption("Ask questions about uploaded PDF, TXT, CSV, and Excel files. Answers are grounded in retrieved evidence.")

with st.sidebar:
    st.subheader("Workspace")
    backend = st.text_input("Backend URL", value="http://127.0.0.1:8000")
    uploaded = st.file_uploader("Upload document", type=["pdf", "txt", "csv", "xlsx", "xls"])
    if uploaded is not None and uploaded.name.lower().endswith((".xlsx", ".xls")):
        st.caption("Excel: uses the first worksheet, with column headers in the first row.")
    if uploaded is not None and st.button("Ingest file"):
        with st.spinner("Ingesting document (embedding can take a minute)..."):
            try:
                files = {"file": (uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")}
                response = httpx.post(
                    api_url(backend, "/documents/upload"),
                    files=files,
                    timeout=UPLOAD_TIMEOUT,
                )
                response.raise_for_status()
                st.success(f"Uploaded {response.json().get('filename')}")
                st.rerun()
            except Exception as exc:
                st.error(request_error_message(exc))

    st.markdown("**Documents**")
    documents, list_error = fetch_documents(backend)
    if list_error:
        st.caption(list_error)
    elif not documents:
        st.caption("No documents uploaded.")
    for doc in documents:
        cols = st.columns([4, 1])
        cols[0].write(f"{doc['filename']} ({doc['file_type']}, {doc['status']})")
        if cols[1].button("Delete", key=f"del-{doc['document_id']}"):
            try:
                response = httpx.delete(
                    api_url(backend, f"/documents/{doc['document_id']}"),
                    timeout=LIST_TIMEOUT,
                )
                response.raise_for_status()
                st.rerun()
            except Exception as exc:
                st.error(request_error_message(exc))

    if st.button("New conversation"):
        st.session_state.conversation_id = None
        st.session_state.messages = []
        st.session_state.pending_question = None
        st.rerun()

if any(doc.get("status") == "processing" for doc in documents):
    st.info("A document is still being ingested (embeddings). Wait until status is ready, then ask again.")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.write(message["content"])
        if message.get("sources"):
            st.markdown("**Sources**")
            for source in message["sources"]:
                st.markdown(f"- {source.get('source_reference') or source.get('filename')}")
                if source.get("excerpt"):
                    st.caption(source["excerpt"])
        if message.get("execution_flow"):
            with st.expander("How this was processed"):
                st.write(" → ".join(message["execution_flow"]))

question = st.chat_input("Ask a question about the documents")
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    st.session_state.pending_question = question
    st.rerun()

if st.session_state.pending_question:
    pending = st.session_state.pending_question
    st.session_state.pending_question = None
    with st.spinner("Answering..."):
        try:
            response = httpx.post(
                api_url(backend, "/chat"),
                json={"question": pending, "conversation_id": st.session_state.conversation_id},
                timeout=CHAT_TIMEOUT,
            )
            response.raise_for_status()
            body = response.json()
            st.session_state.conversation_id = body["conversation_id"]
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": body["answer"],
                    "sources": body.get("sources") or [],
                    "execution_flow": body.get("execution_flow") or [],
                }
            )
        except Exception as exc:
            st.session_state.messages.append(
                {"role": "assistant", "content": request_error_message(exc)}
            )
    st.rerun()
