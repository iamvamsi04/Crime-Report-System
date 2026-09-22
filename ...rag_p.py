from __future__ import annotations

import logging
from typing import Any

from app.llm import generate_structured_response
from app.models import (
    Document,
    PlanStep,
    QueryPlan,
)

log = logging.getLogger(__name__)





def document_summary(
    documents: list[Document],
) -> list[dict[str, Any]]:
    """
    Create a compact document description for the planner.

    The planner receives metadata, not the contents of the
    documents. Actual evidence is retrieved later.
    """

    return [
        {
            "id": document.id,
            "filename": document.filename,
            "file_type": document.file_type,
            "status": document.status,
        }
        for document in documents
        if document.status == "ready"
    ]





async def create_plan(
    question: str,
    documents: list[Document],
    conversation_context: str = "",
) -> QueryPlan:
    """
    Create an execution plan for the user's question.

    The planner decides which document sources are needed,
    but it does not retrieve evidence or execute queries.
    """

    available_documents = document_summary(
        documents
    )

    if not available_documents:
        return QueryPlan(
            steps=[],
            requires_multiple_sources=False,
            requires_calculation=False,
        )

    document_text = "\n".join(
        (
            f"- ID: {item['id']} | "
            f"Filename: {item['filename']} | "
            f"Type: {item['file_type']}"
        )
        for item in available_documents
    )

    prompt = f"""
You are the planning component of a document
analysis system.

Your job is to create an execution plan for a
user's question.

You MUST NOT answer the question.

You MUST decide which uploaded documents need to
be consulted and which operation should be used.

Available operations:

1. "rag"
   Use this for PDF and TXT documents.
   These documents are searched using semantic
   retrieval.

2. "csv_query"
   Use this for CSV and Excel documents.
   These documents are queried using DuckDB.

Rules:

- Only use documents from the available document list.
- Use the document ID exactly as provided.
- Do not invent document IDs.
- Do not invent documents.
- PDF/TXT must use "rag".
- CSV/Excel must use "csv_query".
- If the question requires information from multiple
  documents, create multiple plan steps.
- A question comparing a PDF/TXT value with a CSV
  value normally requires both operations.
- A question asking for calculations over tabular
  data requires "csv_query".
- A question asking about textual facts in a PDF/TXT
  requires "rag".
- A question about information unavailable in the
  available documents should result in an empty plan.
- Follow-up questions should use the conversation
  context to understand references such as:
    "it"
    "that"
    "the previous year"
    "the second document"
    "what about sales?"
- Do not generate SQL.
- Do not generate an answer.
- Do not perform calculations yourself.

Available documents:
{document_text}

Conversation context:
{conversation_context or "No previous conversation context."}

User question:
{question}
"""

    schema = {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "rag",
                                "csv_query",
                            ],
                        },
                        "document_ids": {
                            "type": "array",
                            "items": {
                                "type": "string",
                            },
                        },
                        "purpose": {
                            "type": "string",
                        },
                    },
                    "required": [
                        "action",
                        "document_ids",
                        "purpose",
                    ],
                },
            },
            "requires_multiple_sources": {
                "type": "boolean",
            },
            "requires_calculation": {
                "type": "boolean",
            },
        },
        "required": [
            "steps",
            "requires_multiple_sources",
            "requires_calculation",
        ],
    }

    try:
        result = await generate_structured_response(
            prompt=prompt,
            schema=schema,
        )

        plan = QueryPlan.model_validate(
            result
        )

    except Exception:
        log.exception(
            "Failed to create execution plan."
        )

        raise RuntimeError(
            "The system could not determine how "
            "to process the question."
        )

    validated_plan = validate_plan(
        plan,
        documents,
    )

    log.info(
        "Execution plan created: %s",
        validated_plan.model_dump(
            mode="json"
        ),
    )

    return validated_plan





def validate_plan(
    plan: QueryPlan,
    documents: list[Document],
) -> QueryPlan:
    """
    Validate and normalize the LLM-generated plan.

    The LLM is not trusted to select arbitrary document IDs
    or incompatible operations.
    """

    document_map = {
        document.id: document
        for document in documents
        if document.status == "ready"
    }

    validated_steps: list[PlanStep] = []

    for step in plan.steps:
        valid_document_ids: list[str] = []

        for document_id in step.document_ids:
            document = document_map.get(
                document_id
            )

            if document is None:
                log.warning(
                    "Planner referenced unavailable "
                    "document: %s",
                    document_id,
                )
                continue

            if step.action == "rag":
                if document.file_type not in {
                    "pdf",
                    "txt",
                }:
                    log.warning(
                        "Planner attempted RAG on "
                        "non-text document %s",
                        document.filename,
                    )
                    continue

            elif step.action == "csv_query":
                if document.file_type not in {
                    "csv",
                    "excel",
                }:
                    log.warning(
                        "Planner attempted CSV query on "
                        "non-tabular document %s",
                        document.filename,
                    )
                    continue

            valid_document_ids.append(
                document_id
            )

        if not valid_document_ids:
            continue

        validated_steps.append(
            PlanStep(
                action=step.action,
                document_ids=valid_document_ids,
                purpose=step.purpose.strip(),
            )
        )

    return QueryPlan(
        steps=validated_steps,
        requires_multiple_sources=(
            len(
                {
                    document_id
                    for step in validated_steps
                    for document_id in step.document_ids
                }
            )
            > 1
        ),
        requires_calculation=plan.requires_calculation,
    )





def describe_plan(
    plan: QueryPlan,
    documents: list[Document],
) -> list[str]:
    """
    Convert the internal plan into safe, user-visible
    execution-flow messages.

    This intentionally does not expose the model's
    hidden reasoning.
    """

    document_map = {
        document.id: document
        for document in documents
    }

    flow: list[str] = []

    if not plan.steps:
        flow.append(
            "No suitable uploaded document was identified "
            "for this question."
        )

        return flow

    for step in plan.steps:
        filenames = [
            document_map[document_id].filename
            for document_id in step.document_ids
            if document_id in document_map
        ]

        if step.action == "rag":
            flow.append(
                "Selected document text retrieval for: "
                + ", ".join(filenames)
            )

        elif step.action == "csv_query":
            flow.append(
                "Selected tabular data analysis for: "
                + ", ".join(filenames)
            )

    if plan.requires_multiple_sources:
        flow.append(
            "Multiple document sources are required."
        )

    if plan.requires_calculation:
        flow.append(
            "The question requires a calculation "
            "from the retrieved data."
        )

    return flow
