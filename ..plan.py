from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.gemini import GeminiService
from app.models import (
    ConversationContext,
    ExecutionMode,
    QueryPlan,
)
from app.storage import Storage

log = logging.getLogger(__name__)


STRUCTURED_TYPES = {"csv", "xlsx", "xls"}
TEXT_TYPES = {"pdf", "txt"}


PLAN_SYSTEM = """
You are the query-routing and evidence-planning component of a document
question-answering system.

The user asks questions about documents that have already been uploaded.

Your job is NOT to answer the question.

Your job is NOT to decide whether the computation is SUM, AVG, GROUP BY,
RANK, YOY, percentage change, correlation, median, or any other predefined
operation.

Your job is to determine what evidence paths are required.

There are exactly three execution modes:

1. "retrieval"
   Use this when the answer should be obtained from document text/evidence,
   especially PDF or TXT documents.

2. "structured"
   Use this when answering requires querying, calculating, filtering,
   comparing, aggregating, inspecting, summarizing, or otherwise analyzing
   CSV/Excel table data.

3. "hybrid"
   Use this when the question requires BOTH:
   - textual evidence from PDF/TXT/document chunks, and
   - structured analysis from CSV/Excel data.

Return JSON only.

Use this schema:

{
  "mode": "retrieval | structured | hybrid",

  "is_follow_up": false,
  "out_of_scope": false,
  "ambiguous": false,
  "ambiguity_reason": null,

  "document_ids": [],
  "document_hints": [],

  "retrieval_query": null,
  "structured_question": null,

  "entities": [],
  "metrics": [],
  "years": [],
  "filters": {}
}

DOCUMENT SELECTION

You will receive a DOCUMENT CATALOG containing:
- document ID
- filename
- document type
- structured schema information when available

Use document IDs from that catalog only.

If the user explicitly names a file, select the corresponding document.

If the question clearly refers to one document based on its filename or
context, select it.

If multiple documents are required, include all relevant document IDs.

If the question can reasonably apply across multiple uploaded documents,
select the documents needed to answer it.

Do not invent document IDs or filenames.

RETRIEVAL MODE

Use "retrieval" for questions whose answer should come from textual document
content.

Examples:
- What does the policy say about remote work?
- Summarize the CEO's discussion of risk.
- What reason does the report give for the delay?
- Compare the strategy described in these two reports.
- What are the main conclusions in the PDF?

For retrieval mode:
- set retrieval_query to a concise semantic search query
- structured_question should normally be null

STRUCTURED MODE

Use "structured" whenever the question requires examining CSV/Excel data.

This includes BOTH calculations and direct table lookups.

Examples:
- What is total revenue?
- What department is Alice in?
- Show John's salary.
- How many rows are there?
- Which product appears most often?
- Average salary by department.
- Find duplicate customer IDs.
- Which column has the most missing values?
- What percentage of sales came from the top 10 customers?
- Which region grew the fastest?
- Calculate CAGR.
- Find correlations.
- Summarize this dataset.
- Are there any unusual patterns?
- Compare 2024 and 2025.
- Which records satisfy these conditions?
- Give me the third-highest value.
- Calculate the median.
- Show monthly trends.
- Compare two spreadsheets.

Do NOT attempt to decompose these into predefined operations.

Instead, put the complete analytical request into "structured_question".

The downstream structured-analysis engine receives the real dataset schemas
and dynamically determines the required SQL.

For structured mode:
- structured_question should contain a complete standalone analytical task
- retrieval_query should normally be null

HYBRID MODE

Use "hybrid" when both document text and structured data are required.

Examples:
- The annual report says revenue should grow 15%. Did the CSV achieve that?
- Compare the targets in the PDF with actual results in the spreadsheet.
- Does the employee data support the staffing claim made in the report?
- According to the policy document, which spreadsheet records violate the
  stated threshold?

For hybrid mode:
- retrieval_query should describe the textual evidence to retrieve
- structured_question should describe the calculation/query needed from the
  table data

FOLLOW-UP QUESTIONS

The conversation context may contain:
- previous entities
- previous metrics
- previous years
- previous document IDs
- previous document hints
- previous execution mode
- previous question
- previous answer

Resolve references such as:
- it
- that
- those
- them
- the same department
- that year
- what about 2025?
- and Marketing?
- which one was highest?
- compare that with 2024

Use previous context only when the current question genuinely depends on it.

A follow-up's structured_question must still be rewritten as a standalone
analytical task whenever possible.

Example:

Previous question:
"What was Engineering revenue in 2024?"

Current:
"What about 2025?"

structured_question:
"What was Engineering revenue in 2025?"

Do not blindly copy previous entities/years when the new question replaces
them.

OUT OF SCOPE

Set out_of_scope=true only when the user's request is clearly unrelated to
the available documents or asks for external information that cannot be
derived from them.

Do NOT mark a question out of scope merely because you do not immediately
know how to calculate it. The structured analysis engine can perform complex,
multi-step SQL.

AMBIGUITY

Set ambiguous=true only when a missing distinction materially prevents
choosing the evidence/documents needed to answer correctly.

Do not mark a question ambiguous merely because it is complex.

If there is one structured document and the user asks "what is the average?",
the downstream analysis may still need a specific column; that can be
ambiguous.

If the user asks "summarize this spreadsheet" and there is one spreadsheet,
that is NOT ambiguous.

ENTITIES / METRICS / YEARS / FILTERS

These fields are lightweight conversational context only.

They do not control analytical capabilities.

Extract them when clearly useful, but do not invent them and do not force
every question into these fields.

CRITICAL PRINCIPLE

Your responsibility is routing and evidence selection.

Never reduce an arbitrary analytical question to a fixed operation vocabulary.
"""


def build_plan(
    question: str,
    store: Storage,
    gemini: GeminiService,
    context: ConversationContext | None = None,
) -> QueryPlan:
    """
    Build a high-level evidence plan.

    Unlike the old planner, this does not emit executable Pandas operations.
    """

    question = question.strip()

    if not question:
        return QueryPlan(
            ambiguous=True,
            ambiguity_reason="The question was empty.",
        )

    documents = store.list_documents(ready_only=True)

    if not documents:
        return QueryPlan(
            out_of_scope=True,
            ambiguity_reason="No ready documents are available.",
        )

    catalog = _document_catalog(documents)

    user_prompt = _planner_prompt(
        question=question,
        catalog=catalog,
        context=context,
    )

    try:
        payload = gemini.generate_json(
            system=PLAN_SYSTEM,
            user=user_prompt,
        )

        plan = QueryPlan.model_validate(payload)

    except Exception:
        log.exception("query_plan_generation_failed")

        # The fallback intentionally performs only routing/document selection.
        # It does NOT attempt to infer SUM/GROUPBY/etc.
        plan = _fallback_plan(
            question=question,
            documents=documents,
            context=context,
        )

    plan = _normalize_plan(
        plan=plan,
        question=question,
        documents=documents,
        context=context,
    )

    log.info(
        "query_plan mode=%s follow_up=%s docs=%s ambiguous=%s out_of_scope=%s",
        plan.mode,
        plan.is_follow_up,
        plan.document_ids,
        plan.ambiguous,
        plan.out_of_scope,
    )

    return plan


def _planner_prompt(
    *,
    question: str,
    catalog: list[dict[str, Any]],
    context: ConversationContext | None,
) -> str:
    parts = [
        "CURRENT QUESTION:",
        question,
        "",
        "DOCUMENT CATALOG:",
        json.dumps(
            catalog,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
    ]

    if context is not None:
        parts.extend(
            [
                "",
                "CONVERSATION CONTEXT:",
                json.dumps(
                    context.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
            ]
        )

    parts.extend(
        [
            "",
            (
                "Create the evidence-routing plan. Do not answer the "
                "question and do not generate analytical SQL."
            ),
        ]
    )

    return "\n".join(parts)


def _document_catalog(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Build the planner's view of uploaded documents.

    For structured files we expose enough schema information to help the
    planner understand which file is relevant.

    Detailed runtime schemas are still rebuilt from the actual dataframe by
    dataset.py before SQL generation.
    """

    catalog: list[dict[str, Any]] = []

    for document in documents:
        document_id = str(document["id"])
        filename = str(document["filename"])
        file_type = _document_type(document)

        entry: dict[str, Any] = {
            "document_id": document_id,
            "filename": filename,
            "file_type": file_type,
        }

        if file_type in STRUCTURED_TYPES:
            profile = _parse_profile(
                document.get("csv_profile")
            )

            if profile:
                entry["structured_profile"] = _compact_profile(
                    profile
                )

        catalog.append(entry)

    return catalog


def _compact_profile(
    profile: dict[str, Any],
) -> dict[str, Any]:
    """
    Keep the planning prompt useful without injecting the entire ingestion
    profile.

    This function accepts both the old profile format and the richer profile
    that ingest.py will provide later.
    """

    compact: dict[str, Any] = {}

    for key in (
        "row_count",
        "column_count",
        "columns",
        "dtypes",
        "numeric_columns",
        "date_columns",
        "categorical_columns",
        "sample_values",
    ):
        if key in profile:
            compact[key] = profile[key]

    # Future/richer ingestion profiles may already contain a structured
    # columns array.
    if isinstance(profile.get("column_profiles"), list):
        compact["column_profiles"] = profile[
            "column_profiles"
        ]

    return compact


def _normalize_plan(
    *,
    plan: QueryPlan,
    question: str,
    documents: list[dict[str, Any]],
    context: ConversationContext | None,
) -> QueryPlan:
    """
    Normalize model output against the real document catalog.

    Gemini cannot introduce arbitrary document IDs.
    """

    by_id = {
        str(document["id"]): document
        for document in documents
    }

    plan.document_ids = [
        document_id
        for document_id in plan.document_ids
        if document_id in by_id
    ]

    plan.document_hints = _valid_document_hints(
        hints=plan.document_hints,
        documents=documents,
    )

    # Resolve filename hints into IDs where possible.
    hinted_ids = _ids_for_hints(
        hints=plan.document_hints,
        documents=documents,
    )

    for document_id in hinted_ids:
        if document_id not in plan.document_ids:
            plan.document_ids.append(document_id)

    # If Gemini selected no document, use deterministic routing where there is
    # an obvious single candidate.
    if not plan.document_ids:
        candidates = _documents_for_mode(
            mode=plan.mode,
            documents=documents,
        )

        if len(candidates) == 1:
            plan.document_ids = [
                str(candidates[0]["id"])
            ]

    # Follow-up document inheritance. Only use it when the current planner
    # failed to identify documents.
    if (
        not plan.document_ids
        and context is not None
        and context.document_ids
    ):
        inherited = [
            document_id
            for document_id in context.document_ids
            if document_id in by_id
        ]

        if inherited:
            plan.document_ids = inherited
            plan.is_follow_up = True

    # Ensure each execution mode has the corresponding natural-language task.
    if plan.mode == ExecutionMode.STRUCTURED:
        if not plan.structured_question:
            plan.structured_question = question

        plan.retrieval_query = None

    elif plan.mode == ExecutionMode.RETRIEVAL:
        if not plan.retrieval_query:
            plan.retrieval_query = question

        plan.structured_question = None

    elif plan.mode == ExecutionMode.HYBRID:
        if not plan.retrieval_query:
            plan.retrieval_query = question

        if not plan.structured_question:
            plan.structured_question = question

    # Do not let an inherited context accidentally select the wrong document
    # type for a new execution mode.
    plan.document_ids = _filter_ids_for_mode(
        document_ids=plan.document_ids,
        mode=plan.mode,
        documents=documents,
    )

    # If filtering removed everything, try deterministic candidates once more.
    if not plan.document_ids:
        candidates = _documents_for_mode(
            mode=plan.mode,
            documents=documents,
        )

        if len(candidates) == 1:
            plan.document_ids = [
                str(candidates[0]["id"])
            ]

    return plan


def _fallback_plan(
    *,
    question: str,
    documents: list[dict[str, Any]],
    context: ConversationContext | None,
) -> QueryPlan:
    """
    Conservative fallback used only when Gemini planning fails.

    There is deliberately no keyword -> analytical operation map here.

    We only infer whether structured and/or textual evidence is likely needed.
    """

    mentioned = _documents_mentioned_in_question(
        question=question,
        documents=documents,
    )

    if mentioned:
        mentioned_types = {
            _document_type(document)
            for document in mentioned
        }

        has_structured = bool(
            mentioned_types.intersection(
                STRUCTURED_TYPES
            )
        )

        has_text = bool(
            mentioned_types.intersection(
                TEXT_TYPES
            )
        )

        if has_structured and has_text:
            mode = ExecutionMode.HYBRID

        elif has_structured:
            mode = ExecutionMode.STRUCTURED

        else:
            mode = ExecutionMode.RETRIEVAL

        return QueryPlan(
            mode=mode,
            document_ids=[
                str(document["id"])
                for document in mentioned
            ],
            document_hints=[
                str(document["filename"])
                for document in mentioned
            ],
            retrieval_query=(
                question
                if mode in {
                    ExecutionMode.RETRIEVAL,
                    ExecutionMode.HYBRID,
                }
                else None
            ),
            structured_question=(
                question
                if mode in {
                    ExecutionMode.STRUCTURED,
                    ExecutionMode.HYBRID,
                }
                else None
            ),
        )

    structured = [
        document
        for document in documents
        if _document_type(document)
        in STRUCTURED_TYPES
    ]

    textual = [
        document
        for document in documents
        if _document_type(document)
        in TEXT_TYPES
    ]

    # If only one evidence family exists, routing is deterministic.
    if structured and not textual:
        return QueryPlan(
            mode=ExecutionMode.STRUCTURED,
            document_ids=[
                str(document["id"])
                for document in structured
            ],
            structured_question=question,
        )

    if textual and not structured:
        return QueryPlan(
            mode=ExecutionMode.RETRIEVAL,
            document_ids=[
                str(document["id"])
                for document in textual
            ],
            retrieval_query=question,
        )

    # Reuse previous mode/documents when this strongly looks like a follow-up.
    if (
        context is not None
        and context.last_execution_mode is not None
        and context.document_ids
        and _looks_like_follow_up(question)
    ):
        mode = context.last_execution_mode

        return QueryPlan(
            mode=mode,
            is_follow_up=True,
            document_ids=list(
                context.document_ids
            ),
            document_hints=list(
                context.document_hints
            ),
            retrieval_query=(
                question
                if mode in {
                    ExecutionMode.RETRIEVAL,
                    ExecutionMode.HYBRID,
                }
                else None
            ),
            structured_question=(
                question
                if mode in {
                    ExecutionMode.STRUCTURED,
                    ExecutionMode.HYBRID,
                }
                else None
            ),
        )

    # When both evidence families exist and planning failed, guessing could
    # silently answer from the wrong source. Mark it ambiguous instead.
    return QueryPlan(
        mode=ExecutionMode.RETRIEVAL,
        ambiguous=True,
        ambiguity_reason=(
            "The system could not determine which uploaded document "
            "or data source should be used for this question."
        ),
    )


def _documents_for_mode(
    *,
    mode: ExecutionMode,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if mode == ExecutionMode.STRUCTURED:
        return [
            document
            for document in documents
            if _document_type(document)
            in STRUCTURED_TYPES
        ]

    if mode == ExecutionMode.RETRIEVAL:
        return [
            document
            for document in documents
            if _document_type(document)
            in TEXT_TYPES
        ]

    # Hybrid needs both families. Returning all relevant documents allows the
    # downstream retrieval and dataset loaders to take their respective types.
    return [
        document
        for document in documents
        if _document_type(document)
        in STRUCTURED_TYPES.union(TEXT_TYPES)
    ]


def _filter_ids_for_mode(
    *,
    document_ids: list[str],
    mode: ExecutionMode,
    documents: list[dict[str, Any]],
) -> list[str]:
    by_id = {
        str(document["id"]): document
        for document in documents
    }

    result: list[str] = []

    for document_id in document_ids:
        document = by_id.get(document_id)

        if document is None:
            continue

        file_type = _document_type(document)

        if (
            mode == ExecutionMode.STRUCTURED
            and file_type not in STRUCTURED_TYPES
        ):
            continue

        if (
            mode == ExecutionMode.RETRIEVAL
            and file_type not in TEXT_TYPES
        ):
            continue

        if (
            mode == ExecutionMode.HYBRID
            and file_type
            not in STRUCTURED_TYPES.union(TEXT_TYPES)
        ):
            continue

        if document_id not in result:
            result.append(document_id)

    return result


def _valid_document_hints(
    *,
    hints: list[str],
    documents: list[dict[str, Any]],
) -> list[str]:
    filenames = [
        str(document["filename"])
        for document in documents
    ]

    result: list[str] = []

    for hint in hints:
        for filename in filenames:
            if _filename_matches(
                filename=filename,
                hint=hint,
            ):
                if filename not in result:
                    result.append(filename)

                break

    return result


def _ids_for_hints(
    *,
    hints: list[str],
    documents: list[dict[str, Any]],
) -> list[str]:
    result: list[str] = []

    for hint in hints:
        for document in documents:
            filename = str(
                document["filename"]
            )

            if _filename_matches(
                filename=filename,
                hint=hint,
            ):
                document_id = str(
                    document["id"]
                )

                if document_id not in result:
                    result.append(document_id)

    return result


def _documents_mentioned_in_question(
    *,
    question: str,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lowered = question.casefold()

    matches: list[dict[str, Any]] = []

    for document in documents:
        filename = str(
            document["filename"]
        )

        stem = Path(filename).stem

        if (
            filename.casefold() in lowered
            or (
                len(stem) >= 3
                and stem.casefold() in lowered
            )
        ):
            matches.append(document)

    return matches


def _filename_matches(
    *,
    filename: str,
    hint: str,
) -> bool:
    filename = filename.strip().casefold()
    hint = hint.strip().casefold()

    if not filename or not hint:
        return False

    if filename == hint:
        return True

    filename_stem = Path(filename).stem
    hint_stem = Path(hint).stem

    return (
        filename_stem == hint_stem
        or hint in filename
    )


def _document_type(
    document: dict[str, Any],
) -> str:
    file_type = str(
        document.get("file_type") or ""
    ).strip().lower().lstrip(".")

    if file_type:
        return file_type

    return Path(
        str(document.get("filename") or "")
    ).suffix.lower().lstrip(".")


def _parse_profile(
    raw: Any,
) -> dict[str, Any] | None:
    if not raw:
        return None

    if isinstance(raw, dict):
        return raw

    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    return (
        parsed
        if isinstance(parsed, dict)
        else None
    )


def _looks_like_follow_up(
    question: str,
) -> bool:
    """
    This heuristic is intentionally about conversational dependency only.
    It is NOT an analytical-operation heuristic.
    """

    text = question.strip().casefold()

    patterns = (
        r"^(and|also|then)\b",
        r"^what about\b",
        r"^how about\b",
        r"^what if\b",
        r"\b(that|those|them|same|previous)\b",
        r"\bthat year\b",
        r"\bthat department\b",
        r"\bthat file\b",
        r"\bthat document\b",
    )

    return any(
        re.search(pattern, text)
        for pattern in patterns
    )
