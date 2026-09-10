"""Simple Streamlit client for the FastAPI document-analysis backend."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# `streamlit run ui/streamlit_app.py` makes ui/ the script root on Windows.
# Add the project root so the sibling `app` package is importable regardless of
# the current working directory used to launch Streamlit.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.exceptions import RuntimeConfigurationError
from app.core.runtime import validate_ui_dependency

try:
    validate_ui_dependency()
    import httpx
    import streamlit as st
except (ImportError, RuntimeConfigurationError) as exc:
    print(
        "Streamlit startup configuration failed: activate the documented Python 3.11 virtual environment "
        "and run 'python -m pip install -r requirements.txt'.",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")


def _request(method: str, endpoint: str, **kwargs):
    try:
        response = httpx.request(method, f"{API_BASE_URL}{endpoint}", timeout=60, **kwargs)
        response.raise_for_status()
        return response.json(), None
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", exc.response.text)
        except ValueError:
            detail = exc.response.text
        return None, str(detail)
    except httpx.HTTPError as exc:
        return None, f"Could not reach the API at {API_BASE_URL}: {exc}"


def main() -> None:
    st.set_page_config(page_title="Document Analysis", page_icon="📄", layout="wide")
    st.title("Intelligent Document Analysis")
    st.caption("Answers are grounded in local documents. Gemini receives only selected evidence and calculation results.")

    if "session_id" not in st.session_state:
        payload, error = _request("POST", "/sessions")
        if error:
            st.error(error)
            return
        st.session_state.session_id = payload["session_id"]

    with st.sidebar:
        st.subheader("Documents")
        uploaded_files = st.file_uploader("Upload PDF, TXT, or CSV", type=["pdf", "txt", "csv"], accept_multiple_files=True)
        if st.button("Upload files", disabled=not uploaded_files):
            files = [("files", (item.name, item.getvalue(), item.type or "application/octet-stream")) for item in uploaded_files]
            payload, error = _request("POST", "/documents/upload", files=files)
            if error:
                st.error(error)
            else:
                st.success(f"Ingested {len(payload['documents'])} file(s).")
        if st.button("Load local documents/"):
            payload, error = _request("POST", "/documents/load-local")
            if error:
                st.error(error)
            else:
                st.success(f"Loaded {len(payload['documents'])} local file(s).")
        payload, error = _request("GET", "/documents")
        if not error:
            for document in payload:
                st.caption(f"• {document['filename']} ({document['document_type']})")

    question = st.chat_input("Ask a question about the available documents")
    if question:
        with st.chat_message("user"):
            st.write(question)
        with st.chat_message("assistant"):
            with st.spinner("Retrieving evidence and running local analysis..."):
                payload, error = _request(
                    "POST", "/questions",
                    json={"question": question, "session_id": st.session_state.session_id},
                )
            if error:
                st.error(error)
                return
            st.write(payload["answer"])
            st.subheader("Execution flow")
            for step in payload["execution_flow"]:
                st.write(f"{step['label']} → {step['detail']}")
            if payload["conflicts"]:
                st.warning("Conflicting information detected")
                for conflict in payload["conflicts"]:
                    st.write(conflict["description"])
            if payload["sources"]:
                st.subheader("Sources")
                for source in payload["sources"]:
                    location = []
                    if source.get("page_number"):
                        location.append(f"page {source['page_number']}")
                    if source.get("line_start"):
                        location.append(f"lines {source['line_start']}–{source['line_end']}")
                    if source.get("columns"):
                        location.append("columns: " + ", ".join(source["columns"]))
                    st.markdown(f"- **{source['filename']}**" + (f" ({'; '.join(location)})" if location else ""))
                    if source.get("excerpt"):
                        st.caption(source["excerpt"])
                    if source.get("calculation_details"):
                        st.caption(source["calculation_details"])


if __name__ == "__main__":
    main()
