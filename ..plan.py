from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.errors import GeminiError
from app.evidence import MISSING_ANSWER
from app.gemini import GeminiService
from app.models import (
    ConversationContext,
    Intent,
    PlanOp,
    QueryPlan,
)

log = logging.getLogger(__name__)


STRUCTURED_TYPES = {
    "csv",
    "xlsx",
    "xls",
}


PLAN_SYSTEM = """
You are the planning component of an intelligent document-analysis system.

Your job is to understand the user's question and create a JSON execution plan.

The system supports:

1. PDF and TXT documents
   - searched using semantic RAG retrieval.

2. CSV and Excel documents
   - queried using DuckDB.
   - The SQL itself is generated later by another LLM component.
   - Never assume a fixed set of products, departments, entities, dates,
     or columns.

The supplied document catalog contains the actual available documents and,
for structured documents, their actual column information.

Return JSON only.

Use exactly this structure:

{
  "intent": "<Intent value>",
  "is_follow_up": false,
  "conversation_intent": "normal",
  "resolved_question": null,
  "history_target": null,
  "out_of_scope": false,
  "entities": [],
  "metrics": [],
  "years": [],
  "filters": {},
  "document_ids": [],
  "document_hints": [],
  "operations": [],
  "ambiguous": false,
  "ambiguity_reason": null,
  "retrieval_query": null
}

Valid Intent values:

factual_lookup
numerical_lookup
csv_aggregation
sum
average
minimum
maximum
ranking
sorting
filtering
percentage_change
year_over_year_comparison
entity_comparison
document_comparison
multi_document_question
follow_up_question
missing_information
conflicting_information
out_of_scope
ambiguous

Valid operation values:

retrieve
load_csv
sum
average
min
max
count
sort
rank
filter
groupby
percentage_change
yoy
compare

============================================================
STRUCTURED DATA RULES
============================================================

CSV and Excel questions MUST use DuckDB.

For a question whose answer depends on CSV/Excel data:

- include load_csv
- identify the relevant structured document
- identify the relevant columns from the supplied schema
- use filter operations when the question contains constraints
- use an aggregation/calculation operation when required
- do not use retrieve as a substitute for querying structured data

Examples:

Question:
"What was the total revenue in 2024?"

Plan conceptually:

load_csv
filter Year = 2024
sum Revenue

Question:
"How many units were sold for Carretera on 1/10/2014?"

Plan conceptually:

load_csv
filter Product = Carretera
filter Date = 1/10/2014
sum Units Sold

Question:
"What is the average profit for the Sales department?"

Plan conceptually:

load_csv
filter Department = Sales
average Profit

Question:
"Which department had the highest profit?"

Plan conceptually:

load_csv
group/rank using Department and Profit

Do NOT calculate the answer yourself.

Do NOT invent SQL.

Do NOT invent column names.

Do NOT invent values.

Do NOT assume a particular dataset.

If the catalog does not contain enough information to identify the
required structured data, mark the plan ambiguous or missing_information.

============================================================
UNSTRUCTURED DOCUMENT RULES
============================================================

For questions whose answer comes from PDF/TXT content:

- use retrieve
- provide a retrieval_query
- identify document_ids/document_hints when the question names a document

Do not use CSV operations merely because CSV files happen to exist.

============================================================
MIXED QUESTIONS
============================================================

A question can require both structured and unstructured information.

Example:

"According to the annual report, what risks were mentioned, and how
did revenue change between 2023 and 2024?"

Use:

retrieve
load_csv
yoy or percentage_change

The final answer will combine the evidence and analysis results.

============================================================
FOLLOW-UP QUESTIONS
============================================================

Use the supplied conversation context.

Examples:

Previous:
"What was the revenue of Sales in 2023?"

Follow-up:
"What about 2024?"

Resolve it as a question about Sales revenue in 2024.

Previous:
"Which department had the highest profit?"

Follow-up:
"What was its revenue?"

Resolve "its" using the previous context.

Set:

"is_follow_up": true
"conversation_intent": "follow_up"

and provide "resolved_question" containing the complete question
after resolving the context.

Do not invent missing context.

============================================================
HISTORY / REPEAT
============================================================

If the user asks about the conversation itself, use:

conversation_intent = "history"

Examples:

"What did I ask before?"
"What was my previous question?"

If the user asks to repeat the previous answer:

conversation_intent = "repeat"

These requests do not require document retrieval.

============================================================
DOCUMENT SELECTION
============================================================

Use document_ids only from the supplied catalog.

Use document_hints only with filenames actually present in the catalog.

If the question clearly names a document, select that document.

If there is only one suitable document, it may be selected automatically.

If multiple documents could satisfy the question and the question does not
identify which one is intended, mark the plan ambiguous rather than guessing.

============================================================
ENTITY AND COLUMN RULES
============================================================

Entities must come from the user's question or conversation context.

Never use a hardcoded entity list.

Metrics should correspond to actual columns in the supplied schema whenever
the question is about structured data.

For example, if the schema contains:

["Product", "Units Sold", "Revenue", "Profit"]

and the question says:

"units sold for Product A"

then:

metric = "Units Sold"
entity/filter value = "Product A"

Do NOT confuse the requested metric with the entity value.

============================================================
DATES AND YEARS
============================================================

Preserve dates from the user.

If a structured document contains a suitable date column, use a filter.

If a structured document contains a year column and the question specifies
a year, use a filter.

For "previous year", "last year", or "prior year", use conversation context
when available.

============================================================
OUT OF SCOPE
============================================================

If the question cannot reasonably be answered using the supplied documents,
mark:

out_of_scope = true
intent = out_of_scope

Do not use outside knowledge.

============================================================
AMBIGUITY
============================================================

If there are multiple plausible documents, columns, or interpretations and
the available context cannot resolve them, mark:

ambiguous = true

and explain the ambiguity in ambiguity_reason.

Do not guess.
"""


def build_plan(
    *,
    question: str,
    context: ConversationContext,
    documents: list[dict[str, Any]],
    gemini: GeminiService,
) -> QueryPlan:
    question = question.strip()

    if not question:
        raise ValueError("Question cannot be empty.")

    catalog = _build_catalog(documents)

    payload = {
        "question": question,
        "conversation_context": context.model_dump(),
        "documents": catalog,
    }

    user_prompt = json.dumps(
        payload,
        default=str,
        ensure_ascii=False,
    )

    log.info(
        "question_classification documents=%s follow_up_context=%s",
        len(catalog),
        bool(context.last_question),
    )

    try:
        raw = gemini.generate_json(
            PLAN_SYSTEM,
            user_prompt,
        )
    except Exception as exc:
        log.exception("question_planning_failed")
        raise GeminiError(
            "Could not understand the question."
        ) from exc

    try:
        plan = QueryPlan.model_validate(raw)
    except Exception as exc:
        log.exception(
            "invalid_query_plan raw=%r",
            raw,
        )
        raise GeminiError(
            "Question understanding returned an invalid plan."
        ) from exc

    plan = _normalize_plan(
        plan=plan,
        question=question,
        context=context,
        documents=documents,
    )

    log.info(
        "query_planning intent=%s operations=%s",
        plan.intent,
        [operation.op for operation in plan.operations],
    )

    return plan


def _build_catalog(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []

    for document in documents:
        item: dict[str, Any] = {
            "document_id": document.get("id"),
            "filename": document.get("filename"),
            "file_type": document.get("file_type"),
        }

        file_type = str(
            document.get(
                "file_type",
                "",
            )
        ).lower()

        if file_type in STRUCTURED_TYPES:
            profile = _parse_profile(
                document.get("csv_profile")
            )

            item["structured"] = True
            item["columns"] = profile.get(
                "columns",
                [],
            )
            item["numeric_columns"] = profile.get(
                "numeric_columns",
                [],
            )
            item["entity_columns"] = profile.get(
                "entity_columns",
                [],
            )
            item["year_columns"] = profile.get(
                "year_columns",
                [],
            )
            item["date_columns"] = profile.get(
                "date_columns",
                [],
            )
            item["categorical_columns"] = profile.get(
                "categorical_columns",
                [],
            )
            item["row_count"] = profile.get(
                "row_count"
            )

        else:
            item["structured"] = False

        catalog.append(item)

    return catalog


def _normalize_plan(
    *,
    plan: QueryPlan,
    question: str,
    context: ConversationContext,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    plan = _normalize_conversation(
        plan,
        question,
        context,
    )

    if plan.conversation_intent in {
        "history",
        "repeat",
    }:
        plan.operations = []
        return plan

    known_documents = {
        str(document["id"]): document
        for document in documents
    }

    plan.document_ids = [
        document_id
        for document_id in plan.document_ids
        if str(document_id) in known_documents
    ]

    plan.document_hints = _normalize_document_hints(
        plan.document_hints,
        documents,
    )

    plan = _normalize_structured_operations(
        plan=plan,
        question=question,
        context=context,
        documents=documents,
    )

    plan = _normalize_retrieval(
        plan=plan,
        documents=documents,
    )

    if (
        not plan.operations
        and not plan.out_of_scope
        and not plan.ambiguous
    ):
        plan.operations = [
            PlanOp(op="retrieve")
        ]

    return plan


def _normalize_conversation(
    plan: QueryPlan,
    question: str,
    context: ConversationContext,
) -> QueryPlan:
    conversation_intent = (
        plan.conversation_intent
        or "normal"
    ).strip().lower()

    if conversation_intent in {
        "history",
        "repeat",
    }:
        plan.conversation_intent = conversation_intent
        return plan

    follow_up = (
        plan.is_follow_up
        or plan.intent == Intent.FOLLOW_UP_QUESTION
        or _looks_like_follow_up(question)
    )

    if not follow_up:
        plan.conversation_intent = "normal"
        return plan

    plan.is_follow_up = True
    plan.conversation_intent = "follow_up"

    if not plan.resolved_question:
        plan.resolved_question = _resolve_follow_up_text(
            question,
            context,
        )

    if not plan.entities:
        plan.entities = list(
            context.entities
        )

    if not plan.metrics:
        plan.metrics = list(
            context.metrics
        )

    if not plan.years:
        plan.years = _resolve_follow_up_years(
            question,
            context,
        )

    if not plan.document_ids:
        plan.document_ids = list(
            context.document_ids
        )

    if not plan.document_hints:
        plan.document_hints = list(
            context.document_hints
            if hasattr(context, "document_hints")
            else []
        )

    if plan.intent == Intent.FOLLOW_UP_QUESTION:
        if context.last_intent:
            try:
                plan.intent = Intent(
                    context.last_intent
                )
            except ValueError:
                pass

    return plan


def _normalize_structured_operations(
    *,
    plan: QueryPlan,
    question: str,
    context: ConversationContext,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    structured_documents = [
        document
        for document in documents
        if str(
            document.get(
                "file_type",
                "",
            )
        ).lower()
        in STRUCTURED_TYPES
    ]

    if not structured_documents:
        return plan

    if not _plan_targets_structured_data(
        plan,
        structured_documents,
    ):
        return plan

    selected = _select_structured_document(
        plan,
        structured_documents,
    )

    if selected is None:
        if len(structured_documents) == 1:
            selected = structured_documents[0]
        else:
            return plan

    profile = _parse_profile(
        selected.get("csv_profile")
    )

    columns = [
        str(column)
        for column in profile.get(
            "columns",
            [],
        )
    ]

    if not columns:
        return plan

    plan.document_ids = [
        str(selected["id"])
    ]

    plan.document_hints = [
        selected["filename"]
    ]

    # The LLM is responsible for understanding the question.
    # These deterministic checks only make sure that the resulting
    # operations reference real schema columns.
    operations = _sanitize_structured_operations(
        plan.operations,
        columns,
    )

    if not any(
        operation.op == "load_csv"
        for operation in operations
    ):
        operations.insert(
            0,
            PlanOp(
                op="load_csv",
                filename_hint=selected["filename"],
            ),
        )

    operations = _ensure_schema_filters(
        operations=operations,
        question=question,
        plan=plan,
        profile=profile,
    )

    operations = _ensure_structured_operation(
        operations=operations,
        question=question,
        plan=plan,
        profile=profile,
    )

    plan.operations = _deduplicate_operations(
        operations
    )

    return plan


def _plan_targets_structured_data(
    plan: QueryPlan,
    structured_documents: list[dict[str, Any]],
) -> bool:
    if any(
        operation.op == "load_csv"
        for operation in plan.operations
    ):
        return True

    structured_operations = {
        "sum",
        "average",
        "min",
        "max",
        "count",
        "sort",
        "rank",
        "filter",
        "groupby",
        "percentage_change",
        "yoy",
        "compare",
    }

    if any(
        operation.op in structured_operations
        for operation in plan.operations
    ):
        return True

    structured_intents = {
        Intent.NUMERICAL_LOOKUP,
        Intent.CSV_AGGREGATION,
        Intent.SUM,
        Intent.AVERAGE,
        Intent.MINIMUM,
        Intent.MAXIMUM,
        Intent.RANKING,
        Intent.SORTING,
        Intent.FILTERING,
        Intent.PERCENTAGE_CHANGE,
        Intent.YEAR_OVER_YEAR_COMPARISON,
        Intent.ENTITY_COMPARISON,
    }

    if plan.intent in structured_intents:
        return True

    return False


def _select_structured_document(
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for document_id in plan.document_ids:
        for document in documents:
            if str(document["id"]) == str(document_id):
                return document

    for hint in plan.document_hints:
        normalized = hint.casefold()

        for document in documents:
            filename = str(
                document["filename"]
            ).casefold()

            if normalized in filename:
                return document

    if len(documents) == 1:
        return documents[0]

    return None


def _sanitize_structured_operations(
    operations: list[PlanOp],
    columns: list[str],
) -> list[PlanOp]:
    result: list[PlanOp] = []

    for operation in operations:
        if operation.op == "retrieve":
            continue

        if operation.op == "load_csv":
            result.append(operation)
            continue

        column = operation.column

        if column:
            column = _resolve_column(
                columns,
                column,
            )

            if column is None:
                # Do not allow the LLM to invent a column.
                column = None

        result.append(
            operation.model_copy(
                update={
                    "column": column,
                }
            )
        )

    return result


def _ensure_schema_filters(
    *,
    operations: list[PlanOp],
    question: str,
    plan: QueryPlan,
    profile: dict[str, Any],
) -> list[PlanOp]:
    columns = [
        str(column)
        for column in profile.get(
            "columns",
            [],
        )
    ]

    existing = {
        (
            operation.column or "",
            str(operation.value),
        )
        for operation in operations
        if operation.op == "filter"
    }

    result = list(operations)

    # Preserve explicit LLM-generated filters.
    # Add year filters only when the question explicitly contains a year
    # and the dataset has a year column.
    years = _years_in_text(question)

    year_columns = [
        str(column)
        for column in profile.get(
            "year_columns",
            [],
        )
    ]

    if years and year_columns:
        key = (
            year_columns[0],
            str(years[0]),
        )

        if key not in existing:
            result.append(
                PlanOp(
                    op="filter",
                    column=year_columns[0],
                    value=years[0],
                )
            )

    # Preserve the planner's entity understanding.
    # We do NOT attempt to discover arbitrary entity values by scanning
    # the dataset. DuckDB will handle the actual lookup later.
    entity_columns = [
        str(column)
        for column in profile.get(
            "entity_columns",
            [],
        )
    ]

    if plan.entities and entity_columns:
        entity_column = _best_entity_column(
            columns=columns,
            entity_columns=entity_columns,
            question=question,
        )

        if entity_column:
            value = plan.entities[0]
            key = (
                entity_column,
                str(value),
            )

            if key not in existing:
                result.append(
                    PlanOp(
                        op="filter",
                        column=entity_column,
                        value=value,
                    )
                )

    return result


def _ensure_structured_operation(
    *,
    operations: list[PlanOp],
    question: str,
    plan: QueryPlan,
    profile: dict[str, Any],
) -> list[PlanOp]:
    if any(
        operation.op in {
            "sum",
            "average",
            "min",
            "max",
            "count",
            "sort",
            "rank",
            "groupby",
            "percentage_change",
            "yoy",
            "compare",
        }
        for operation in operations
    ):
        return operations

    numeric_columns = [
        str(column)
        for column in profile.get(
            "numeric_columns",
            [],
        )
    ]

    metric = _resolve_metric(
        plan.metrics,
        numeric_columns,
        question,
    )

    if not metric:
        return operations

    aggregation = _infer_aggregation(
        question
    )

    result = list(operations)

    result.append(
        PlanOp(
            op=aggregation,
            column=metric,
            value_column=metric,
        )
    )

    return result


def _resolve_metric(
    metrics: list[str],
    numeric_columns: list[str],
    question: str,
) -> str | None:
    for metric in metrics:
        resolved = _resolve_column(
            numeric_columns,
            metric,
        )

        if resolved:
            return resolved

    question_lower = question.casefold()

    best: tuple[int, str] | None = None

    for column in numeric_columns:
        normalized = column.casefold()

        score = 0

        if normalized in question_lower:
            score += 100

        for token in re.findall(
            r"[a-z0-9]+",
            normalized,
        ):
            if len(token) >= 3 and token in question_lower:
                score += 10

        if score:
            if best is None or score > best[0]:
                best = (
                    score,
                    column,
                )

    return best[1] if best else None


def _infer_aggregation(
    question: str,
) -> str:
    q = question.casefold()

    if any(
        token in q
        for token in (
            "average",
            "avg",
            "mean",
        )
    ):
        return "average"

    if any(
        token in q
        for token in (
            "minimum",
            "lowest",
            "smallest",
        )
    ):
        return "min"

    if any(
        token in q
        for token in (
            "maximum",
            "highest",
            "largest",
        )
    ):
        return "max"

    if any(
        token in q
        for token in (
            "how many rows",
            "how many records",
            "number of rows",
            "number of records",
        )
    ):
        return "count"

    return "sum"


def _normalize_retrieval(
    *,
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    if plan.out_of_scope or plan.ambiguous:
        return plan

    structured_ids = {
        str(document["id"])
        for document in documents
        if str(
            document.get(
                "file_type",
                "",
            )
        ).lower()
        in STRUCTURED_TYPES
    }

    rag_ids = {
        str(document["id"])
        for document in documents
        if str(
            document.get(
                "file_type",
                "",
            )
        ).lower()
        not in STRUCTURED_TYPES
    }

    has_structured_operation = any(
        operation.op != "retrieve"
        for operation in plan.operations
    )

    has_retrieve = any(
        operation.op == "retrieve"
        for operation in plan.operations
    )

    if has_structured_operation and has_retrieve:
        # Mixed question: keep both.
        return plan

    if has_structured_operation:
        # Structured-only question.
        plan.operations = [
            operation
            for operation in plan.operations
            if operation.op != "retrieve"
        ]
        return plan

    if has_retrieve:
        return plan

    # A question without an explicit operation should retrieve from
    # unstructured documents when such documents exist.
    if rag_ids:
        plan.operations = [
            PlanOp(op="retrieve")
        ]

    return plan


def _normalize_document_hints(
    hints: list[str],
    documents: list[dict[str, Any]],
) -> list[str]:
    filenames = [
        str(document["filename"])
        for document in documents
    ]

    result: list[str] = []

    for hint in hints:
        normalized = hint.casefold()

        for filename in filenames:
            if (
                normalized == filename.casefold()
                or normalized in filename.casefold()
                or filename.casefold() in normalized
            ):
                if filename not in result:
                    result.append(filename)

                break

    return result


def _parse_profile(
    raw: Any,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw

    if not raw:
        return {}

    try:
        value = json.loads(raw)

        if isinstance(value, dict):
            return value

    except (
        json.JSONDecodeError,
        TypeError,
    ):
        pass

    return {}


def _resolve_column(
    columns: list[str],
    requested: str,
) -> str | None:
    if not requested:
        return None

    target = _normalize_name(
        requested
    )

    for column in columns:
        if _normalize_name(column) == target:
            return column

    return None


def _normalize_name(
    value: str,
) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        value.casefold(),
    ).strip()


def _best_entity_column(
    *,
    columns: list[str],
    entity_columns: list[str],
    question: str,
) -> str | None:
    q = question.casefold()

    for column in entity_columns:
        if (
            "product" in column.casefold()
            and "product" in q
        ):
            return column

    for column in entity_columns:
        if (
            "department" in column.casefold()
            and "department" in q
        ):
            return column

    for column in entity_columns:
        if column in columns:
            return column

    return None


def _deduplicate_operations(
    operations: list[PlanOp],
) -> list[PlanOp]:
    result: list[PlanOp] = []
    seen: set[str] = set()

    for operation in operations:
        key = json.dumps(
            operation.model_dump(
                by_alias=True,
                exclude_none=True,
            ),
            sort_keys=True,
            default=str,
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(operation)

    return result


def _resolve_follow_up_text(
    question: str,
    context: ConversationContext,
) -> str:
    if not context.last_question:
        return question

    previous = context.last_question.strip()

    return (
        f"Previous question: {previous}\n"
        f"Follow-up question: {question}\n"
        "Resolve the follow-up using the previous conversation context."
    )


def _resolve_follow_up_years(
    question: str,
    context: ConversationContext,
) -> list[int]:
    years = _years_in_text(question)

    if years:
        return years

    q = question.casefold()

    if any(
        token in q
        for token in (
            "previous year",
            "last year",
            "prior year",
        )
    ):
        if context.years:
            return [
                context.years[-1] - 1
            ]

    return list(
        context.years
    )


def _looks_like_follow_up(
    question: str,
) -> bool:
    q = question.strip().casefold()

    if any(
        phrase in q
        for phrase in (
            "previous year",
            "last year",
            "prior year",
            "the difference",
            "compare that",
            "what about",
            "how about",
            "same for",
        )
    ):
        return True

    if len(q.split()) <= 10 and q.startswith(
        (
            "and ",
            "now ",
            "what about ",
            "how about ",
            "same ",
        )
    ):
        return True

    return False


def _years_in_text(
    text: str,
) -> list[int]:
    return [
        int(year)
        for year in re.findall(
            r"\b(?:19|20)\d{2}\b",
            text,
        )
    ]
