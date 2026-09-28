from __future__ import annotations

import json
import logging
from typing import Any

from app.csv_analysis import get_profile
from app.llm import generate_structured_response
from app.models import Document, PlanStep, QueryPlan

log = logging.getLogger(__name__)


def document_summary(
    documents: list[Document],
) -> list[dict[str, Any]]:
    """Create compact metadata for planner document selection."""

    summaries: list[dict[str, Any]] = []

    for document in documents:
        if document.status != "ready":
            continue

        summary: dict[str, Any] = {
            "id": document.id,
            "filename": document.filename,
            "file_type": document.file_type,
        }

        if document.file_type in {"csv", "excel", "xlsx", "xls"}:
            profile = get_profile(document.id)

            if profile is not None:
                summary["profile"] = profile.model_dump(
                    mode="json"
                )
            else:
                log.warning(
                    "No CSV profile found for document %s",
                    document.id,
                )

        summaries.append(summary)

    return summaries


async def create_plan(
    question: str,
    documents: list[Document],
    conversation_context: str = "",
) -> QueryPlan:
    """
    Ask the LLM to select relevant document IDs only.
    Python assigns operations using each document's file type.
    """

    available_documents = document_summary(documents)

    if not available_documents:
        return QueryPlan(
            steps=[],
            requires_multiple_sources=False,
            requires_calculation=False,
        )

    document_text = json.dumps(
        available_documents,
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""
You are a document selection planner.

Select the uploaded document IDs needed to answer the
CURRENT USER QUESTION.

Do NOT answer the question.
Do NOT generate SQL.
Do NOT perform calculations.
Do NOT choose operations.

DOCUMENT SELECTION

1. Understand the subject and intent of the current question.
   Identify what information is needed to answer it.

2. Match that information to the filenames and available
   metadata. Select only documents that can reasonably
   contain the required information.

3. For CSV/Excel documents:

   * Select them when the question asks for facts, values,
     comparisons, filtering, grouping, or calculations
     that can be answered using the table's columns.
   * Check whether the table's columns and profile are
     relevant to the actual information requested.
   * A shared word, country name, date, or entity is not
     enough to make a table relevant.
   * Do not use sales, units, or profit data to explain
     historical events or economic causes unless the
     question explicitly asks for an analysis of that data.

4. For PDF/TXT documents:

   * Select documents whose topics match the subject
     and information requested.
   * Questions asking for explanations, causes, reasons,
     background, or historical context generally require
     relevant explanatory documents.
   * Use the filename and available metadata to identify
     documents likely to contain that information.

5. If multiple documents are needed, select all clearly
   relevant documents. Do not add documents merely
   because they might contain loosely related information.

6. Do not select unrelated documents.
   If no uploaded document is clearly relevant, return
   an empty document ID list.

7. Use conversation context only to resolve references
   or understand follow-up questions. Select documents
   based on the current question after resolving its
   meaning.

8. For a history-only question, return no document IDs.
   A short question is not automatically history-only.

EXAMPLES

* "Why 150 prepositions" → english_club.pdf
* "One-word Prepositions" → english_club.pdf
* "Quiz 19 6th question" → english_club.pdf
* "What happened to the economy in 2014?"
  → 2014_economy.txt.txt
* "What was the profit in 2014?"
  → Financials.csv, if its profile contains the
  relevant data.
* "Reason for China slowdown"
  → a relevant economic or historical PDF/TXT document,
  if available. Do not select a sales CSV merely
  because it contains country-level data.
* "Which country sold the most Carretera units?"
  → Financials.csv, if its profile contains the
  relevant columns.
* "What SQL did you use?"
  → no document IDs.

AVAILABLE DOCUMENTS
{document_text}

CONVERSATION CONTEXT
{conversation_context}

CURRENT USER QUESTION
{question}

Return only the structured response.

"""

    schema = {
        "type": "object",
        "properties": {
            "document_ids": {
                "type": "array",
                "items": {
                    "type": "string",
                },
            },
            "requires_calculation": {
                "type": "boolean",
            },
        },
        "required": [
            "document_ids",
            "requires_calculation",
        ],
    }

    try:
        print(document_text)
        result = await generate_structured_response(
            prompt=prompt,
            schema=schema,
        )

        selected_ids = result.get("document_ids", [])
        requires_calculation = result.get(
            "requires_calculation",
            False,
        )

        if not isinstance(selected_ids, list):
            raise ValueError(
                "Planner document_ids must be a list."
            )

        plan = build_plan_from_selected_ids(
            selected_ids=selected_ids,
            documents=documents,
            requires_calculation=requires_calculation,
        )

    except Exception:
        log.exception("Failed to create execution plan.")
        raise RuntimeError(
            "The system could not determine how "
            "to process the question."
        )

    log.info(
        "Execution plan created: %s",
        plan.model_dump(mode="json"),
    )

    return plan


def build_plan_from_selected_ids(
    selected_ids: list[str],
    documents: list[Document],
    requires_calculation: bool = False,
) -> QueryPlan:
    """
    Validate selected IDs and assign operations deterministically.
    """

    document_map = {
        str(document.id): document
        for document in documents
        if document.status == "ready"
    }

    rag_ids: list[str] = []
    csv_ids: list[str] = []

    for raw_id in selected_ids:
        document_id = str(raw_id).strip()
        document = document_map.get(document_id)

        if document is None:
            log.warning(
                "Planner referenced unavailable document: %s",
                document_id,
            )
            continue

        file_type = (
            str(document.file_type)
            .lower()
            .strip()
            .lstrip(".")
        )

        if file_type in {"pdf", "txt"}:
            if document_id not in rag_ids:
                rag_ids.append(document_id)

        elif file_type in {"csv", "excel", "xlsx", "xls"}:
            if document_id not in csv_ids:
                csv_ids.append(document_id)

        else:
            log.warning(
                "Unsupported file type for document %s: %s",
                document.filename,
                file_type,
            )

    steps: list[PlanStep] = []

    if rag_ids:
        steps.append(
            PlanStep(
                action="rag",
                document_ids=rag_ids,
                purpose="Retrieve relevant information from text documents.",
            )
        )

    if csv_ids:
        steps.append(
            PlanStep(
                action="csv_query",
                document_ids=csv_ids,
                purpose="Analyze relevant tabular data.",
            )
        )

    return QueryPlan(
        steps=steps,
        requires_multiple_sources=(
            len(rag_ids) + len(csv_ids) > 1
        ),
        requires_calculation=bool(requires_calculation),
    )


def validate_plan(
    plan: QueryPlan,
    documents: list[Document],
) -> QueryPlan:
    """
    Compatibility helper for existing callers.

    Rebuild operations from document types rather than trusting
    the operations in the supplied plan.
    """

    selected_ids = [
        document_id
        for step in plan.steps
        for document_id in step.document_ids
    ]

    return build_plan_from_selected_ids(
        selected_ids=selected_ids,
        documents=documents,
        requires_calculation=plan.requires_calculation,
    )


def describe_plan(
    plan: QueryPlan,
    documents: list[Document],
) -> list[str]:
    """Create safe, user-visible execution-flow messages."""

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
