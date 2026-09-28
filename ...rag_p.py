from __future__ import annotations

import logging
from typing import Any
from app.csv_analysis import get_profile

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

    For CSV/Excel documents, include the saved CSV profile
    so the planner can identify relevant tabular documents
    using column names and sample values.
    """

    summaries: list[dict[str, Any]] = []

    for document in documents:

        if document.status != "ready":
            continue

        summary: dict[str, Any] = {
            "id": document.id,
            "filename": document.filename,
            "file_type": document.file_type,
            "status": document.status,
        }

        if document.file_type in {
            "csv",
            "excel",
        }:
            profile = get_profile(
                document.id
            )

            if profile is not None:
                summary["profile"] = (
                    profile.model_dump(
                        mode="json"
                    )
                )
            else:
                log.warning(
                    "No CSV profile found for document %s",
                    document.id,
                )

        summaries.append(
            summary
        )

    return summaries





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
You are a document routing planner.

Determine which document(s) are relevant to the CURRENT USER QUESTION and which operation should be used.

Do NOT answer the question.
Do NOT generate SQL.
Do NOT perform calculations.

OPERATION ASSIGNMENT — HARD RULE
The operation is determined ONLY by file type:

PDF  → "rag"
TXT  → "rag"
CSV  → "csv_query"
XLSX → "csv_query"
XLS  → "csv_query"

Never use "csv_query" for PDF/TXT.
Never use "rag" for CSV/Excel.

DOCUMENT SELECTION
First understand what the CURRENT USER QUESTION is about.
Then select the document whose subject is relevant.

Use filenames and metadata as semantic clues.

For CSV/Excel:
- Select only when the question clearly concerns tabular data, columns, values, or calculations from that document.
- Do not select a CSV merely because the question contains a number or generic word.

For PDF/TXT:
- Select the document whose subject matches the question.
- If one document clearly matches, select ONLY that document.
- If multiple documents clearly match, select those documents.
- Do not select unrelated documents.
- Do not select a document merely because it contains a number or word from the question.

CONVERSATION
Use conversation context only to resolve references or follow-ups.
History-only questions require no document-analysis steps.
Do not reuse an unrelated document from a previous question.

DECISION ORDER
1. Understand the current question.
2. Identify its subject.
3. Select relevant document(s).
4. Assign operation strictly from file type.

Examples:
- "Why 150 prepositions" → english_club.pdf → rag
- "Quiz 19 6th question" → english_club.pdf → rag
- "Answers to Prepositions Quizzes" → english_club.pdf → rag
- "What happened to the economy in 2014?" → 2014_economy.txt.txt → rag
- "Why did the economy slow down in 2014?" → 2014_economy.txt.txt → rag
- "What was the profit in 2014?" → Financials.csv → csv_query
- "What SQL did you use?" → no document-analysis step
- anything about prepositions → english_club.pdf → rag

AVAILABLE DOCUMENTS:
{document_text}

CONVERSATION CONTEXT:
{conversation_context}

CURRENT USER QUESTION:
{question}

Return only the structured execution plan.


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
