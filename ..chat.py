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


# ============================================================
# Main chat entry point
# ============================================================


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

    # --------------------------------------------------------
    # Conversation
    # --------------------------------------------------------

    conversation_id = (
        conversation.get_or_create_conversation(
            conversation_id
        )
    )

    # IMPORTANT:
    # Get context before storing the current question.
    # Otherwise the current question would appear twice
    # in the planner context.
    previous_context = (
        conversation.build_context_for_llm(
            conversation_id
        )
    )

    conversation.add_user_message(
        conversation_id=conversation_id,
        content=question,
    )

    execution_flow: list[str] = []

    execution_flow.append(
        "Received the question and loaded the "
        "conversation context."
    )

    # --------------------------------------------------------
    # Available documents
    # --------------------------------------------------------

    available_documents = (
        document_service.list_documents()
    )

    ready_documents = [
        document
        for document in available_documents
        if document.status == "ready"
    ]

    if not ready_documents:
        answer = (
            "There are no processed documents available "
            "to answer this question."
        )

        execution_flow.append(
            "No ready documents were available."
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

    execution_flow.append(
        f"Found {len(ready_documents)} ready document(s)."
    )

    # --------------------------------------------------------
    # Planning
    # --------------------------------------------------------

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
        answer = (
            "I could not identify information in the "
            "uploaded documents that can answer this "
            "question."
        )

        execution_flow.append(
            "No executable document-analysis step "
            "was identified."
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

    # --------------------------------------------------------
    # Execute the plan
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Remove duplicate sources
    # --------------------------------------------------------

    sources = deduplicate_sources(
        sources
    )

    # --------------------------------------------------------
    # Generate final answer
    # --------------------------------------------------------

    if not evidence:
        answer = (
            "I could not find enough relevant information "
            "in the uploaded documents to answer that "
            "question."
        )

        execution_flow.append(
            "No relevant evidence was returned by the "
            "selected document-analysis steps."
        )

    else:
        execution_flow.append(
            "Collected evidence from the selected "
            "document sources."
        )

        answer = await generate_grounded_answer(
    question=question,
    evidence=[
        item.model_dump(mode="json")
        for item in evidence
    ],
    conversation_id=conversation_id,
)

        execution_flow.append(
            "Generated a grounded answer from the "
            "retrieved document evidence."
        )

    # --------------------------------------------------------
    # Store assistant response
    # --------------------------------------------------------

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


# ============================================================
# RAG execution
# ============================================================


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


# ============================================================
# CSV execution
# ============================================================


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


# ============================================================
# Source deduplication
# ============================================================


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
