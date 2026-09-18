from __future__ import annotations

from typing import Any

import requests
import streamlit as st


# ---------------------------------------------------------------------------
# Page configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Document Analysis",
    page_icon="📄",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None

if "backend_url" not in st.session_state:
    st.session_state.backend_url = (
        "http://127.0.0.1:8000"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def api_url(
    path: str,
) -> str:
    base = (
        st.session_state.backend_url
        .strip()
        .rstrip("/")
    )

    return (
        f"{base}/{path.lstrip('/')}"
    )


def request_json(
    method: str,
    path: str,
    *,
    timeout: float = 120.0,
    **kwargs: Any,
) -> Any:
    response = requests.request(
        method=method,
        url=api_url(path),
        timeout=timeout,
        **kwargs,
    )

    if response.ok:
        if not response.content:
            return None

        return response.json()

    detail = None

    try:
        payload = response.json()

        if isinstance(
            payload,
            dict,
        ):
            detail = payload.get(
                "detail"
            )

    except ValueError:
        detail = None

    if not detail:
        detail = (
            response.text.strip()
            or (
                f"Request failed with "
                f"status {response.status_code}."
            )
        )

    raise RuntimeError(
        str(detail)
    )


def load_documents() -> list[
    dict[str, Any]
]:
    try:
        payload = request_json(
            "GET",
            "/documents",
            timeout=30.0,
        )

        if isinstance(
            payload,
            list,
        ):
            return payload

    except Exception:
        return []

    return []


def render_sources(
    sources: list[
        dict[str, Any]
    ],
) -> None:
    if not sources:
        return

    st.markdown(
        "**Sources**"
    )

    for source in sources:
        filename = str(
            source.get(
                "filename"
            )
            or "Document"
        )

        reference = (
            source.get(
                "source_reference"
            )
        )

        if not reference:
            page_number = (
                source.get(
                    "page_number"
                )
            )

            section = (
                source.get(
                    "section"
                )
            )

            row_start = (
                source.get(
                    "row_start"
                )
            )

            row_end = (
                source.get(
                    "row_end"
                )
            )

            if page_number is not None:
                reference = (
                    f"Page {page_number}"
                )

            elif section:
                reference = str(
                    section
                )

            elif (
                row_start is not None
                and row_end is not None
            ):
                if row_start == row_end:
                    reference = (
                        f"Row {row_start}"
                    )
                else:
                    reference = (
                        f"Rows "
                        f"{row_start}–{row_end}"
                    )

        if reference:
            st.caption(
                f"{filename} — {reference}"
            )
        else:
            st.caption(
                filename
            )

        excerpt = (
            source.get(
                "excerpt"
            )
        )

        if excerpt:
            st.caption(
                str(excerpt)
            )


def render_execution_flow(
    execution_flow: list[str],
) -> None:
    if not execution_flow:
        return

    with st.expander(
        "Execution flow"
    ):
        for step in execution_flow:
            st.write(
                f"• {step}"
            )


def render_assistant_payload(
    message: dict[str, Any],
) -> None:
    content = str(
        message.get(
            "content"
        )
        or ""
    )

    st.markdown(
        content
    )

    sources = (
        message.get(
            "sources"
        )
        or []
    )

    if isinstance(
        sources,
        list,
    ):
        render_sources(
            sources
        )

    execution_flow = (
        message.get(
            "execution_flow"
        )
        or []
    )

    if isinstance(
        execution_flow,
        list,
    ):
        render_execution_flow(
            [
                str(step)
                for step
                in execution_flow
            ]
        )


def reset_conversation() -> None:
    st.session_state.messages = []
    st.session_state.conversation_id = None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header(
        "Documents"
    )

    backend_url = st.text_input(
        "Backend URL",
        value=(
            st.session_state.backend_url
        ),
    )

    st.session_state.backend_url = (
        backend_url
    )

    uploaded_file = st.file_uploader(
        "Upload a document",
        type=[
            "pdf",
            "txt",
            "csv",
            "xlsx",
            "xls",
        ],
    )

    st.caption(
        "For Excel files, the first worksheet is analyzed."
    )

    if st.button(
        "Ingest document",
        disabled=(
            uploaded_file is None
        ),
        use_container_width=True,
    ):
        if uploaded_file is not None:
            try:
                with st.spinner(
                    "Ingesting document..."
                ):
                    request_json(
                        "POST",
                        "/documents/upload",
                        files={
                            "file": (
                                uploaded_file.name,
                                uploaded_file.getvalue(),
                                uploaded_file.type
                                or (
                                    "application/"
                                    "octet-stream"
                                ),
                            )
                        },
                        timeout=300.0,
                    )

                st.success(
                    "Document ingested."
                )

                st.rerun()

            except Exception as exc:
                st.error(
                    str(exc)
                )

    st.divider()

    documents = (
        load_documents()
    )

    if not documents:
        st.caption(
            "No documents uploaded."
        )

    else:
        for document in documents:
            document_id = str(
                document.get(
                    "id"
                )
                or ""
            )

            filename = str(
                document.get(
                    "filename"
                )
                or "Document"
            )

            file_type = str(
                document.get(
                    "file_type"
                )
                or ""
            ).upper()

            status = str(
                document.get(
                    "status"
                )
                or ""
            )

            col_name, col_delete = (
                st.columns(
                    [
                        5,
                        1,
                    ]
                )
            )

            with col_name:
                st.write(
                    filename
                )

                details = (
                    file_type
                )

                if status:
                    details = (
                        f"{details} · "
                        f"{status}"
                    )

                st.caption(
                    details
                )

            with col_delete:
                if st.button(
                    "×",
                    key=(
                        f"delete_"
                        f"{document_id}"
                    ),
                    help=(
                        "Delete document"
                    ),
                ):
                    try:
                        request_json(
                            "DELETE",
                            (
                                "/documents/"
                                f"{document_id}"
                            ),
                            timeout=60.0,
                        )

                        st.rerun()

                    except Exception as exc:
                        st.error(
                            str(exc)
                        )

    st.divider()

    if st.button(
        "New conversation",
        use_container_width=True,
    ):
        reset_conversation()

        st.rerun()


# ---------------------------------------------------------------------------
# Main chat
# ---------------------------------------------------------------------------

st.title(
    "Document Analysis"
)

for message in (
    st.session_state.messages
):
    role = (
        message.get(
            "role"
        )
        or "assistant"
    )

    with st.chat_message(
        role
    ):
        if role == "assistant":
            render_assistant_payload(
                message
            )
        else:
            st.markdown(
                str(
                    message.get(
                        "content"
                    )
                    or ""
                )
            )


question = st.chat_input(
    "Ask a question about your documents"
)

if question:
    user_message = {
        "role": "user",
        "content": question,
    }

    st.session_state.messages.append(
        user_message
    )

    with st.chat_message(
        "user"
    ):
        st.markdown(
            question
        )

    with st.chat_message(
        "assistant"
    ):
        try:
            with st.spinner(
                "Analyzing..."
            ):
                payload = request_json(
                    "POST",
                    "/chat",
                    json={
                        "question": question,
                        "conversation_id": (
                            st.session_state
                            .conversation_id
                        ),
                    },
                    timeout=300.0,
                )

            if not isinstance(
                payload,
                dict,
            ):
                raise RuntimeError(
                    "The backend returned an invalid response."
                )

            conversation_id = (
                payload.get(
                    "conversation_id"
                )
            )

            if conversation_id:
                st.session_state.conversation_id = (
                    str(
                        conversation_id
                    )
                )

            assistant_message = {
                "role": "assistant",
                "content": str(
                    payload.get(
                        "answer"
                    )
                    or ""
                ),
                "status": (
                    payload.get(
                        "status"
                    )
                ),
                "sources": (
                    payload.get(
                        "sources"
                    )
                    or []
                ),
                "execution_flow": (
                    payload.get(
                        "execution_flow"
                    )
                    or []
                ),
                "query_plan": (
                    payload.get(
                        "query_plan"
                    )
                ),
            }

            st.session_state.messages.append(
                assistant_message
            )

            render_assistant_payload(
                assistant_message
            )

        except Exception as exc:
            error_message = (
                f"Request failed: {exc}"
            )

            st.error(
                error_message
            )

            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": (
                        error_message
                    ),
                    "status": "error",
                    "sources": [],
                    "execution_flow": [],
                }
            )
