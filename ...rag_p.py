from __future__ import annotations

import logging
from typing import Any

from app import conversation
from app import documents as document_service
from app.csv_analysis import answer_csv_question
from app.llm import (
    format_csv_result,
    generate_grounded_answer,
)
from app.models import (
    ChatResponse,
    Document,
    Evidence,
    RetrievedChunk,
    Source,
)
from app.planner import (
    create_plan,
    describe_plan,
)
from app.rag import retrieve

log = logging.getLogger(__name__)





async def process_chat(
    question: str,
    conversation_id: str | None = None,
) -> ChatResponse:
    """
    Process one user question from beginning to end.

    This function is the main application orchestration
    layer. It does not itself perform document parsing,
    vector search, SQL generation, or LLM prompting.
    """

    question = question.strip()

    if not question:
        raise ValueError(
            "Question cannot be empty."
        )



    conversation_id = (
        conversation.get_or_create_conversation(
            conversation_id
        )
    )

    previous_context = (
        conversation.build_context_for_llm(
            conversation_id
        )
    )

    execution_flow: list[str] = []

    execution_flow.append(
        "Received the question and loaded the "
        "conversation context."
    )


    available_documents = (
        document_service.list_documents()
    )

    ready_documents = [
        document
        for document in available_documents
        if document.status == "ready"
    ]

    execution_flow.append(
        f"Found {len(ready_documents)} ready document(s)."
    )


    log.info(
    "Planner input documents: %s",
    [
        {
            "id": document.id,
            "filename": document.filename,
            "file_type": document.file_type,
        }
        for document in available_documents
    ],
)

    plan = await create_plan(
        question=question,
        documents=ready_documents,
        conversation_context=previous_context,
    )

    execution_flow.extend(
        describe_plan(
            plan,
            ready_documents,
        )
    )

    if not plan.steps:
        execution_flow.append(
            "No document-analysis steps were planned."
        )

        answer = await generate_grounded_answer(
            question=question,
            evidence=[],
            conversation_id=conversation_id,
            planner_status="no_planned_steps",
        )

        execution_flow.append(
            "Generated the response using available "
            "conversation context."
        )

        conversation.add_user_message(
            conversation_id=conversation_id,
            content=question,
        )

        conversation.add_assistant_message(
            conversation_id=conversation_id,
            content=answer,
            sources=[],
            execution_flow=execution_flow,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            sources=[],
            execution_flow=execution_flow,
        )

    conversation.add_user_message(
        conversation_id=conversation_id,
        content=question,
    )

    document_map = {
        document.id: document
        for document in ready_documents
    }

    evidence: list[Evidence] = []
    sources: list[Source] = []

    for step in plan.steps:


        if step.action == "rag":

            rag_evidence, rag_sources = (
                await execute_rag_step(
                    question=question,
                    document_ids=step.document_ids,
                    document_map=document_map,
                    execution_flow=execution_flow,
                )
            )

            evidence.extend(
                rag_evidence
            )

            sources.extend(
                rag_sources
            )

        elif step.action == "csv_query":

            csv_evidence, csv_sources = (
                await execute_csv_step(
                    question=question,
                    document_ids=step.document_ids,
                    document_map=document_map,
                    execution_flow=execution_flow,
                )
            )

            evidence.extend(
                csv_evidence
            )

            sources.extend(
                csv_sources
            )

    sources = deduplicate_sources(
        sources
    )

    if not evidence:
        execution_flow.append(
            "No relevant evidence was returned by the "
            "selected document-analysis steps."
        )

        answer = await generate_grounded_answer(
            question=question,
            evidence=[],
            conversation_id=conversation_id,
            planner_status="document_analysis_planned",
        )

        execution_flow.append(
            "Generated the response using the available "
            "conversation context and document evidence."
        )

    else:
        execution_flow.append(
            "Collected evidence from the selected "
            "document sources."
        )

        answer = await generate_grounded_answer(
            question=question,
            evidence=[
                item.model_dump(
                    mode="json"
                )
                for item in evidence
            ],
            conversation_id=conversation_id,
            planner_status="document_analysis_planned",
        )

        execution_flow.append(
            "Generated a grounded answer from the "
            "retrieved document evidence."
        )

    conversation.add_assistant_message(
        conversation_id=conversation_id,
        content=answer,
        sources=sources,
        execution_flow=execution_flow,
    )


    return ChatResponse(
        conversation_id=conversation_id,
        answer=answer,
        sources=sources,
        execution_flow=execution_flow,
    )




async def execute_rag_step(
    question: str,
    document_ids: list[str],
    document_map: dict[str, Document],
    execution_flow: list[str],
) -> tuple[
    list[Evidence],
    list[Source],
]:
    """
    Execute semantic retrieval for PDF/TXT documents.
    """

    valid_ids = [
        document_id
        for document_id in document_ids
        if document_id in document_map
    ]

    if not valid_ids:
        return [], []

    filenames = [
        document_map[document_id].filename
        for document_id in valid_ids
    ]

    execution_flow.append(
        "Searching PDF/TXT content using semantic "
        "retrieval: "
        + ", ".join(filenames)
    )

    try:
        log.info(
    "RAG retrieval: question=%r document_ids=%s",
    question,
    document_ids,
)
        chunks = retrieve(
            query=question,
            document_ids=valid_ids,
        )

    except Exception:
        log.exception(
            "RAG retrieval failed for question."
        )

        execution_flow.append(
            "PDF/TXT retrieval failed."
        )

        raise RuntimeError(
            "Document text retrieval failed."
        )

    if not chunks:
        execution_flow.append(
            "No sufficiently relevant text passages "
            "were found."
        )

        return [], []

    execution_flow.append(
        f"Retrieved {len(chunks)} relevant text passage(s)."
    )

    evidence: list[Evidence] = []
    sources: list[Source] = []

    for chunk in chunks:
        evidence.append(
            build_rag_evidence(
                chunk
            )
        )

        sources.append(
            build_rag_source(
                chunk
            )
        )

    return evidence, sources


def build_rag_evidence(
    chunk: RetrievedChunk,
) -> Evidence:
    metadata: dict[str, Any] = {
        "score": chunk.score,
    }

    if chunk.page is not None:
        metadata["page"] = chunk.page

    if chunk.section:
        metadata["section"] = chunk.section

    if chunk.chunk_id:
        metadata["chunk_id"] = chunk.chunk_id

    return Evidence(
        source_type="rag",
        document_id=chunk.document_id,
        filename=chunk.filename,
        content=chunk.content,
        metadata=metadata,
    )


def build_rag_source(
    chunk: RetrievedChunk,
) -> Source:
    reference_parts = [
        chunk.filename
    ]

    if chunk.page is not None:
        reference_parts.append(
            f"page {chunk.page}"
        )

    if chunk.section:
        reference_parts.append(
            f"section {chunk.section}"
        )

    return Source(
        document_id=chunk.document_id,
        filename=chunk.filename,
        source_reference=" — ".join(
            reference_parts
        ),
        excerpt=chunk.content[:500],
        page=chunk.page,
        section=chunk.section,
    )




async def execute_csv_step(
    question: str,
    document_ids: list[str],
    document_map: dict[str, Document],
    execution_flow: list[str],
) -> tuple[
    list[Evidence],
    list[Source],
]:
    """
    Execute DuckDB analysis for CSV/Excel documents.

    Each selected tabular document is queried independently.
    This keeps the generated SQL scoped to a known source.
    """

    evidence: list[Evidence] = []
    sources: list[Source] = []

    for document_id in document_ids:

        document = document_map.get(
            document_id
        )

        if document is None:
            continue

        execution_flow.append(
            f"Analyzing {document.filename} "
            "with DuckDB."
        )

        try:
            result = await answer_csv_question(
                question=question,
                document=document,
            )

        except Exception:
            log.exception(
                "CSV analysis failed for %s",
                document.filename,
            )

            execution_flow.append(
                f"DuckDB analysis failed for "
                f"{document.filename}."
            )

            raise RuntimeError(
                f"Could not analyze {document.filename}."
            )

        execution_flow.append(
            f"Executed a DuckDB query against "
            f"{document.filename}."
        )

        result_dict = result.model_dump(
            mode="json"
        )

        evidence.append(
            Evidence(
                source_type="csv",
                document_id=document.id,
                filename=document.filename,
                content=format_csv_result(
                    result_dict
                ),
                metadata={
                    "sql": result.sql,
                    "columns": result.columns,
                    "row_count": result.row_count,
                },
            )
        )

        sources.append(
            build_csv_source(
                document,
                result_dict,
            )
        )

    return evidence, sources


def build_csv_source(
    document: Document,
    result: dict[str, Any],
) -> Source:
    row_count = result.get(
        "row_count",
        0,
    )

    columns = result.get(
        "columns",
        [],
    )

    reference = (
        f"{document.filename} — "
        f"DuckDB query result"
    )

    excerpt = (
        f"Query returned {row_count} row(s). "
        f"Columns: "
        f"{', '.join(str(column) for column in columns)}"
    )

    return Source(
        document_id=document.id,
        filename=document.filename,
        source_reference=reference,
        excerpt=excerpt,
    )




def deduplicate_sources(
    sources: list[Source],
) -> list[Source]:
    seen: set[
        tuple[
            str | None,
            str,
            str,
            int | None,
        ]
    ] = set()

    unique_sources: list[Source] = []

    for source in sources:
        key = (
            source.document_id,
            source.filename,
            source.source_reference,
            source.page,
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        unique_sources.append(
            source
        )

    return unique_sources













from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from app import database
from app.models import (
    Conversation,
    ConversationMessage,
    Source,
)

log = logging.getLogger(__name__)





def utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()





def create_conversation() -> str:
    conversation_id = str(
        uuid.uuid4()
    )

    timestamp = utc_now()

    database.create_conversation(
        conversation_id=conversation_id,
        created_at=timestamp,
    )

    log.info(
        "Created conversation %s",
        conversation_id,
    )

    return conversation_id


def get_or_create_conversation(
    conversation_id: str | None,
) -> str:
    if (
        conversation_id
        and database.conversation_exists(
            conversation_id
        )
    ):
        return conversation_id

    return create_conversation()





def add_user_message(
    conversation_id: str,
    content: str,
) -> None:
    timestamp = utc_now()

    database.add_message(
        conversation_id=conversation_id,
        role="user",
        content=content,
        sources=[],
        execution_flow=[],
        created_at=timestamp,
    )

    database.update_conversation_timestamp(
        conversation_id=conversation_id,
        updated_at=timestamp,
    )


def add_assistant_message(
    conversation_id: str,
    content: str,
    sources: list[Source] | None = None,
    execution_flow: list[str] | None = None,
) -> None:
    timestamp = utc_now()

    source_data = [
        source.model_dump(
            mode="json"
        )
        for source in (
            sources or []
        )
    ]

    database.add_message(
        conversation_id=conversation_id,
        role="assistant",
        content=content,
        sources=source_data,
        execution_flow=execution_flow or [],
        created_at=timestamp,
    )

    database.update_conversation_timestamp(
        conversation_id=conversation_id,
        updated_at=timestamp,
    )





def get_conversation(
    conversation_id: str,
) -> Conversation | None:
    if not database.conversation_exists(
        conversation_id
    ):
        return None

    rows = database.get_messages(
        conversation_id
    )

    messages: list[
        ConversationMessage
    ] = []

    for row in rows:
        messages.append(
            ConversationMessage(
                role=row["role"],
                content=row["content"],
                sources=parse_sources(
                    row["sources_json"]
                ),
                execution_flow=parse_execution_flow(
                    row["execution_flow_json"]
                ),
            )
        )

    return Conversation(
        id=conversation_id,
        messages=messages,
    )




def get_recent_context(
    conversation_id: str,
    max_messages: int = 10,
) -> str:
    """
    Return a compact representation of recent conversation
    history.

    This context is supplied to the planner and final
    answer generator.

    It contains previous user/assistant messages, but not
    internal reasoning.
    """

    rows = database.get_messages(
        conversation_id
    )

    if not rows:
        return ""

    recent_rows = rows[
        -max_messages:
    ]

    lines: list[str] = []

    for row in recent_rows:
        role = row["role"]

        content = (
            row["content"]
            .strip()
        )

        if not content:
            continue

        lines.append(
            f"{role.upper()}: {content}"
        )

    return "\n".join(
        lines
    )





def get_last_user_message(
    conversation_id: str,
) -> str | None:
    rows = database.get_messages(
        conversation_id
    )

    for row in reversed(rows):
        if row["role"] == "user":
            return row["content"]

    return None





def parse_sources(
    value: str | None,
) -> list[Source]:
    if not value:
        return []

    try:
        parsed = json.loads(
            value
        )
    except json.JSONDecodeError:
        log.warning(
            "Invalid sources JSON in conversation."
        )
        return []

    if not isinstance(
        parsed,
        list,
    ):
        return []

    sources: list[Source] = []

    for item in parsed:
        if not isinstance(
            item,
            dict,
        ):
            continue

        try:
            sources.append(
                Source.model_validate(
                    item
                )
            )
        except Exception:
            log.warning(
                "Skipping invalid source entry."
            )

    return sources





def parse_execution_flow(
    value: str | None,
) -> list[str]:
    if not value:
        return []

    try:
        parsed = json.loads(
            value
        )
    except json.JSONDecodeError:
        log.warning(
            "Invalid execution-flow JSON."
        )
        return []

    if not isinstance(
        parsed,
        list,
    ):
        return []

    return [
        str(item)
        for item in parsed
    ]





def build_context_for_llm(
    conversation_id: str,
    max_messages: int = 10,
) -> str:
    """
    Build the conversation context passed to the LLM.

    Only user-visible conversation content is included.
    """

    context = get_recent_context(
        conversation_id=conversation_id,
        max_messages=max_messages,
    )

    if not context:
        return (
            "No previous conversation messages."
        )

    return context
