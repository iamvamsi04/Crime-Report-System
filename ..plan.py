from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.errors import GeminiError
from app.evidence import MISSING_ANSWER
from app.gemini import GeminiService
from app.models import ConversationContext, Intent, PlanOp, QueryPlan

log = logging.getLogger(__name__)


PLAN_SYSTEM = """You convert a user question about uploaded documents into JSON only.

Use exactly this schema:

{
  "intent": "<one Intent enum value>",
  "is_follow_up": false,
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

Valid intents:
factual_lookup,
numerical_lookup,
csv_aggregation,
sum,
average,
minimum,
maximum,
ranking,
sorting,
filtering,
percentage_change,
year_over_year_comparison,
entity_comparison,
document_comparison,
multi_document_question,
follow_up_question,
missing_information,
conflicting_information,
out_of_scope,
ambiguous.

Valid operation values:
retrieve,
load_csv,
sum,
average,
min,
max,
count,
sort,
rank,
filter,
groupby,
percentage_change,
yoy,
compare.

IMPORTANT CSV/EXCEL RULES:

1. CSV and Excel files are structured tabular data.
   Use structured analysis operations for questions that ask for values from
   CSV/Excel files.

2. The structured analysis operations are executed by DuckDB.
   Do NOT plan around Pandas execution.

3. Do NOT use retrieve-only for a CSV/Excel numerical question.

4. For an exact numeric lookup in a CSV/Excel file, use:
   load_csv
   + filter operation(s)
   + an aggregation/value operation on the requested numeric column.

5. When the user asks for the value of a numeric column for specific rows,
   use "sum" as the final operation if the filters identify the intended row(s).
   For a single matching row, sum returns that row's numeric value.

6. Examples:

   Question:
   "How many units were sold on 1/10/2014 for the product Carretera?"

   If the catalog contains Product, Date, and Units Sold columns, plan:
   load_csv
   filter Product = Carretera
   filter Date = 1/10/2014
   sum Units Sold

   Question:
   "What was the total revenue in 2013?"

   If the catalog contains Year and Revenue columns, plan:
   load_csv
   filter Year = 2013
   sum Revenue

7. The requested metric must NEVER be used as the filter value.

   For example:
   "units sold for Carretera"
   means:
   metric = Units Sold
   entity/filter value = Carretera

   It does NOT mean:
   filter Units Sold = Carretera.

8. Use column names from the supplied document catalog whenever possible.

9. For dates, preserve the user's date value in the filter.
   The analysis layer will handle the SQL/date representation.

10. For a question asking "how many units", "number of units", or "units sold",
    identify the actual numeric Units Sold column if it exists.
    Do NOT use count unless the user is asking how many rows/records exist.

11. "total revenue", "total sales", etc. mean sum of the corresponding
    numeric column.

12. "average revenue" means average of the Revenue column.

13. "minimum revenue" means min of the Revenue column.

14. "maximum revenue" means max of the Revenue column.

15. If the question contains a year and the dataset has a Year column,
    create a filter operation for that Year.

16. If the question contains a product/entity and the dataset has a matching
    product/entity column, create a filter operation for that value.

17. If the question contains a date and the dataset has a Date column,
    create a filter operation for that date.

18. For ranking questions, use rank and/or sort operations.

19. For sorting questions, use sort.

20. For grouping questions, use groupby.

21. For percentage-change questions, use percentage_change.

22. For year-over-year questions, use yoy.

23. For comparing entities, use compare and/or groupby.

24. Never invent documents, columns, years, entities, or values.

25. For non-CSV/Excel document questions, retrieve relevant evidence normally.

26. If it is a follow-up, set is_follow_up true and resolve missing information
    using the conversation context.

27. If a question requires both CSV calculation and document evidence,
    include both the appropriate CSV operations and retrieve.

28. Never perform arithmetic yourself. The structured analysis layer calculates
    numerical results using DuckDB SQL.

29. document_hints must contain filenames from the supplied catalog when relevant.

30. If a CSV/Excel question requires an exact numeric result, make sure the
    plan contains load_csv and a concrete numeric operation.

31. Use the supplied schema/profile to identify numeric, entity, year, and date
    columns. Do not invent schema information.
"""


def build_plan(
    *,
    question: str,
    context: ConversationContext,
    documents: list[dict[str, Any]],
    gemini: GeminiService,
) -> QueryPlan:
    catalog: list[dict[str, Any]] = []

    for doc in documents:
        item: dict[str, Any] = {
            "document_id": doc["id"],
            "filename": doc["filename"],
            "file_type": doc["file_type"],
        }

        if doc.get("csv_profile"):
            try:
                profile = (
                    json.loads(doc["csv_profile"])
                    if isinstance(doc["csv_profile"], str)
                    else doc["csv_profile"]
                )

                item["columns"] = profile.get("columns") or []
                item["year_columns"] = profile.get("year_columns") or []
                item["entity_columns"] = profile.get("entity_columns") or []
                item["numeric_columns"] = profile.get("numeric_columns") or []
                item["date_columns"] = profile.get("date_columns") or []

            except (json.JSONDecodeError, TypeError):
                pass

        catalog.append(item)

    user = json.dumps(
        {
            "question": question,
            "conversation_context": context.model_dump(),
            "documents": catalog,
        },
        default=str,
    )

    log.info(
        "question_classification documents=%s follow_up_context=%s",
        len(catalog),
        bool(context.last_question),
    )

    raw = gemini.generate_json(
        PLAN_SYSTEM,
        user,
    )

    try:
        plan = QueryPlan.model_validate(raw)
    except Exception as exc:
        raise GeminiError(
            "Question understanding returned an invalid plan."
        ) from exc

    plan = _merge_follow_up(
        plan,
        context,
        question,
    )

    new_entities = {
        e.casefold()
        for e in plan.entities
    } - {
        e.casefold()
        for e in context.entities
    }

    if (
        context.entities
        and new_entities
    ) or context.last_answer == MISSING_ANSWER:
        previous_docs = [
            d
            for d in documents
            if d["id"] in context.document_ids
        ]

        previous_names = {
            d["filename"].casefold()
            for d in previous_docs
        }

        if not any(
            name in question.casefold()
            for name in previous_names
        ):
            if set(plan.document_ids).issubset(
                context.document_ids
            ):
                plan.document_ids = []

            plan.document_hints = [
                h
                for h in plan.document_hints
                if h.casefold() not in previous_names
            ]

    plan = _apply_csv_normalization(
        plan=plan,
        question=question,
        documents=documents,
    )

    plan = _apply_heuristics(
        plan,
        question,
        documents,
    )

    known_ids = {
        d["id"]
        for d in documents
    }

    plan.document_ids = [
        i
        for i in plan.document_ids
        if i in known_ids
    ]

    log.info(
        "query_planning intent=%s ops=%s",
        plan.intent,
        [op.op for op in plan.operations],
    )

    return plan


def _merge_follow_up(
    plan: QueryPlan,
    context: ConversationContext,
    question: str,
) -> QueryPlan:
    follow_up = (
        plan.is_follow_up
        or _looks_like_follow_up(question)
    )

    if not follow_up:
        mentioned = _entities_in_text(question)

        if mentioned:
            plan.entities = mentioned

        return plan

    plan.is_follow_up = True

    mentioned = _entities_in_text(question)
    q = question.lower()

    if "compare" in q or "difference" in q:
        plan.entities = list(
            dict.fromkeys(
                [
                    *mentioned,
                    *plan.entities,
                    *context.entities,
                ]
            )
        )

    elif mentioned:
        plan.entities = mentioned

    elif not plan.entities:
        plan.entities = list(
            context.entities
        )

    if not plan.metrics:
        plan.metrics = list(
            context.metrics
        )

    if any(
        token in q
        for token in (
            "previous year",
            "last year",
            "prior year",
        )
    ) and context.years:
        plan.years = [
            context.years[-1] - 1
        ]

    elif not plan.years:
        years = _years_in_text(question)
        plan.years = (
            years
            or list(context.years)
        )

    if not plan.document_ids:
        plan.document_ids = list(
            context.document_ids
        )

    if plan.intent in {
        Intent.FOLLOW_UP_QUESTION,
        Intent.AMBIGUOUS,
    } and context.last_intent:
        try:
            plan.intent = Intent(
                context.last_intent
            )
        except ValueError:
            plan.intent = Intent.NUMERICAL_LOOKUP

    return plan


def _apply_csv_normalization(
    *,
    plan: QueryPlan,
    question: str,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    """
    Normalize Gemini's plan using the actual CSV/Excel schema.

    This function only constructs the structured analysis plan.
    Actual SQL generation/execution is handled by app.analyze.
    """

    csv_docs = [
        d
        for d in documents
        if d["file_type"] in {
            "csv",
            "excel",
        }
    ]

    if not csv_docs:
        return plan

    if getattr(
        plan,
        "conversation_intent",
        "normal",
    ) in {
        "history",
        "repeat",
    }:
        return plan

    doc = _choose_csv_document(
        plan=plan,
        csv_docs=csv_docs,
    )

    if doc is None:
        return plan

    profile = _get_profile(doc)

    columns = [
        str(c)
        for c in (
            profile.get("columns")
            or []
        )
    ]

    numeric_columns = [
        str(c)
        for c in (
            profile.get("numeric_columns")
            or []
        )
    ]

    entity_columns = [
        str(c)
        for c in (
            profile.get("entity_columns")
            or []
        )
    ]

    year_columns = [
        str(c)
        for c in (
            profile.get("year_columns")
            or []
        )
    ]

    date_columns = [
        str(c)
        for c in (
            profile.get("date_columns")
            or []
        )
    ]

    # The current ingest.py does not populate date_columns.
    # Infer likely date columns from names/dtypes when necessary.
    if not date_columns:
        date_columns = _infer_date_columns(
            columns=columns,
            profile=profile,
        )

    q = question.casefold()

    csv_ops = [
        op
        for op in plan.operations
        if op.op in {
            "load_csv",
            "sum",
            "average",
            "min",
            "max",
            "count",
            "filter",
            "sort",
            "rank",
            "groupby",
            "percentage_change",
            "yoy",
            "compare",
        }
    ]

    metric_column = _find_metric_column(
        question=question,
        columns=columns,
        numeric_columns=numeric_columns,
        existing_metrics=plan.metrics,
    )

    if metric_column:
        plan.metrics = [
            metric_column
        ]

    years = _years_in_text(question)

    if years:
        plan.years = years

    filters: list[PlanOp] = []

    # --------------------------------------------------------
    # Year filter
    # --------------------------------------------------------

    if (
        plan.years
        and year_columns
    ):
        year_column = year_columns[0]

        filters.append(
            PlanOp(
                op="filter",
                column=year_column,
                value=plan.years[0],
            )
        )

    # --------------------------------------------------------
    # Entity filter
    # --------------------------------------------------------

    if (
        plan.entities
        and entity_columns
    ):
        entity_column = _find_entity_column(
            columns=columns,
            entity_columns=entity_columns,
            question=question,
        )

        if entity_column:
            filters.append(
                PlanOp(
                    op="filter",
                    column=entity_column,
                    value=plan.entities[0],
                )
            )

    # --------------------------------------------------------
    # Date filter
    # --------------------------------------------------------

    date_value = _extract_date_value(
        question
    )

    if (
        date_value
        and date_columns
    ):
        filters.append(
            PlanOp(
                op="filter",
                column=date_columns[0],
                value=date_value,
            )
        )

    # --------------------------------------------------------
    # Existing Gemini filters
    # --------------------------------------------------------

    for existing in csv_ops:
        if existing.op != "filter":
            continue

        column = existing.column
        value = existing.value

        if column:
            resolved = _resolve_column_name(
                columns,
                column,
            )

            if resolved:
                column = resolved

        if value is None:
            continue

        if (
            column
            and column not in columns
        ):
            continue

        filters.append(
            PlanOp(
                op="filter",
                column=column,
                value=value,
            )
        )

    filters = _deduplicate_filters(
        filters
    )

    # --------------------------------------------------------
    # Preserve operations that represent complex analytical
    # questions.
    #
    # These should reach analyze.py so Gemini can generate the
    # appropriate DuckDB SQL.
    # --------------------------------------------------------

    complex_ops = {
        "percentage_change",
        "yoy",
        "compare",
        "groupby",
        "rank",
        "sort",
    }

    existing_complex_ops = [
        op
        for op in csv_ops
        if op.op in complex_ops
    ]

    if existing_complex_ops:
        ops: list[PlanOp] = [
            PlanOp(
                op="load_csv",
                filename_hint=doc["filename"],
            )
        ]

        ops.extend(filters)

        # Preserve Gemini's analytical operation.
        for operation in existing_complex_ops:
            repaired = _repair_operation(
                operation=operation,
                columns=columns,
                numeric_columns=numeric_columns,
                year_columns=year_columns,
            )

            ops.append(repaired)

        plan.operations = ops
        plan.document_ids = [
            doc["id"]
        ]
        plan.document_hints = [
            doc["filename"]
        ]

        return plan

    # --------------------------------------------------------
    # Normal numeric question
    # --------------------------------------------------------

    if (
        metric_column
        and _is_numeric_csv_question(
            question=question,
            metric_column=metric_column,
            columns=columns,
        )
    ):
        aggregation = _requested_aggregation(
            question,
            plan,
        )

        ops = [
            PlanOp(
                op="load_csv",
                filename_hint=doc["filename"],
            )
        ]

        ops.extend(filters)

        ops.append(
            PlanOp(
                op=aggregation,
                column=metric_column,
                value_column=metric_column,
            )
        )

        plan.operations = ops

        if aggregation == "sum":
            plan.intent = Intent.SUM

        elif aggregation == "average":
            plan.intent = Intent.AVERAGE

        elif aggregation == "min":
            plan.intent = Intent.MINIMUM

        elif aggregation == "max":
            plan.intent = Intent.MAXIMUM

        elif aggregation == "count":
            plan.intent = Intent.NUMERICAL_LOOKUP

        else:
            plan.intent = Intent.CSV_AGGREGATION

        plan.document_ids = [
            doc["id"]
        ]

        plan.document_hints = [
            doc["filename"]
        ]

        return plan

    return plan


def _repair_operation(
    *,
    operation: PlanOp,
    columns: list[str],
    numeric_columns: list[str],
    year_columns: list[str],
) -> PlanOp:
    column = operation.column

    if column:
        resolved = _resolve_column_name(
            columns,
            column,
        )

        if resolved:
            column = resolved

    value_column = operation.value_column

    if value_column:
        resolved_value = _resolve_column_name(
            numeric_columns or columns,
            value_column,
        )

        if resolved_value:
            value_column = resolved_value

    year_column = operation.year_column

    if year_column:
        resolved_year = _resolve_column_name(
            year_columns or columns,
            year_column,
        )

        if resolved_year:
            year_column = resolved_year

    return PlanOp(
        op=operation.op,
        target=operation.target,
        filename_hint=operation.filename_hint,
        column=column,
        value=operation.value,
        value_column=value_column,
        year_column=year_column,
        from_year=operation.from_year,
        to_year=operation.to_year,
        ascending=operation.ascending,
        n=operation.n,
        agg=operation.agg,
    )


def _is_numeric_csv_question(
    *,
    question: str,
    metric_column: str,
    columns: list[str],
) -> bool:
    if not metric_column:
        return False

    if metric_column not in columns:
        return False

    q = question.casefold()

    numeric_terms = (
        "how many",
        "number of",
        "units sold",
        "total",
        "sum",
        "average",
        "avg",
        "mean",
        "minimum",
        "maximum",
        "revenue",
        "sales",
        "profit",
        "cost",
        "expense",
        "amount",
        "value",
    )

    return any(
        term in q
        for term in numeric_terms
    )


def _find_metric_column(
    *,
    question: str,
    columns: list[str],
    numeric_columns: list[str],
    existing_metrics: list[str],
) -> str | None:
    for metric in existing_metrics:
        resolved = _resolve_column_name(
            numeric_columns or columns,
            metric,
        )

        if (
            resolved
            and resolved in columns
        ):
            return resolved

    q = question.casefold()

    candidates: list[tuple[int, str]] = []

    for column in numeric_columns:
        normalized = column.casefold()

        score = 0

        if normalized in q:
            score += 100

        words = [
            word
            for word in re.split(
                r"[^a-z0-9]+",
                normalized,
            )
            if word
        ]

        for word in words:
            if (
                len(word) >= 3
                and word in q
            ):
                score += 10

        if normalized in {
            "units sold",
            "units",
            "quantity",
            "qty",
        } and (
            "units sold" in q
            or "number of units" in q
            or "how many units" in q
            or "units" in q
        ):
            score += 80

        if (
            "revenue" in normalized
            and (
                "revenue" in q
                or "sales revenue" in q
            )
        ):
            score += 80

        if (
            "profit" in normalized
            and "profit" in q
        ):
            score += 80

        if (
            "sales" in normalized
            and "sales" in q
        ):
            score += 60

        if score:
            candidates.append(
                (
                    score,
                    column,
                )
            )

    if candidates:
        candidates.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        return candidates[0][1]

    return None


def _find_entity_column(
    *,
    columns: list[str],
    entity_columns: list[str],
    question: str,
) -> str | None:
    if not entity_columns:
        return None

    q = question.casefold()

    for column in entity_columns:
        normalized = column.casefold()

        if (
            "product" in normalized
            and "product" in q
        ):
            return column

    return entity_columns[0]


def _requested_aggregation(
    question: str,
    plan: QueryPlan,
) -> str:
    q = question.casefold()

    if any(
        term in q
        for term in (
            "average",
            "avg",
            "mean",
        )
    ):
        return "average"

    if any(
        term in q
        for term in (
            "minimum",
            "minimum value",
            "lowest",
            "smallest",
        )
    ):
        return "min"

    if any(
        term in q
        for term in (
            "maximum",
            "maximum value",
            "highest",
            "largest",
        )
    ):
        return "max"

    if (
        "how many rows" in q
        or "how many records" in q
        or "number of records" in q
        or "number of rows" in q
    ):
        return "count"

    return "sum"


def _choose_csv_document(
    *,
    plan: QueryPlan,
    csv_docs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for document_id in plan.document_ids:
        for doc in csv_docs:
            if doc["id"] == document_id:
                return doc

    for hint in plan.document_hints:
        hint_lower = hint.casefold()

        for doc in csv_docs:
            if (
                hint_lower
                in doc["filename"].casefold()
            ):
                return doc

    return (
        csv_docs[0]
        if csv_docs
        else None
    )


def _get_profile(
    doc: dict[str, Any],
) -> dict[str, Any]:
    raw = doc.get(
        "csv_profile"
    )

    if not raw:
        return {}

    if isinstance(raw, dict):
        return raw

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


def _infer_date_columns(
    *,
    columns: list[str],
    profile: dict[str, Any],
) -> list[str]:
    """
    The current ingest.py does not store date_columns in csv_profile.

    Infer likely date columns from column names and dtypes so the planner
    can still construct a date filter without requiring an ingest change.
    """

    dtypes = profile.get(
        "dtypes"
    ) or {}

    result: list[str] = []

    date_name_terms = (
        "date",
        "datetime",
        "timestamp",
        "period",
    )

    for column in columns:
        lowered = column.casefold()

        if any(
            term in lowered
            for term in date_name_terms
        ):
            result.append(column)
            continue

        dtype = str(
            dtypes.get(
                column,
                "",
            )
        ).casefold()

        if (
            "datetime" in dtype
            or "date" in dtype
        ):
            result.append(column)

    return result


def _resolve_column_name(
    columns: list[str],
    name: str,
) -> str | None:
    if not name:
        return None

    target = name.strip().casefold()

    for column in columns:
        if column.casefold() == target:
            return column

    normalized_target = re.sub(
        r"[^a-z0-9]+",
        " ",
        target,
    ).strip()

    for column in columns:
        normalized_column = re.sub(
            r"[^a-z0-9]+",
            " ",
            column.casefold(),
        ).strip()

        if (
            normalized_column
            == normalized_target
        ):
            return column

    for column in columns:
        lowered = column.casefold()

        if (
            target in lowered
            or lowered in target
        ):
            return column

    return None


def _deduplicate_filters(
    filters: list[PlanOp],
) -> list[PlanOp]:
    result: list[PlanOp] = []
    seen: set[tuple[str, str]] = set()

    for op in filters:
        if not op.column:
            continue

        key = (
            op.column.casefold(),
            str(op.value).casefold(),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(op)

    return result


def _extract_date_value(
    question: str,
) -> str | None:
    match = re.search(
        r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b",
        question,
    )

    if match:
        return match.group(0)

    match = re.search(
        r"\b(?:"
        r"january|february|march|april|may|june|july|august|"
        r"september|october|november|december"
        r")\s+\d{1,2}(?:st|nd|rd|th)?"
        r"(?:,\s*|\s+)\d{4}\b",
        question,
        re.I,
    )

    if match:
        return match.group(0)

    match = re.search(
        r"\b\d{1,2}(?:st|nd|rd|th)?\s+"
        r"(?:january|february|march|april|may|june|july|august|"
        r"september|october|november|december)"
        r"(?:\s+|,\s*)\d{4}\b",
        question,
        re.I,
    )

    if match:
        return match.group(0)

    return None


def _apply_heuristics(
    plan: QueryPlan,
    question: str,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    """
    Lightweight deterministic normalization.

    The actual numerical work is NOT performed here.
    DuckDB performs the calculation after analyze.py generates SQL.
    """

    if getattr(
        plan,
        "conversation_intent",
        "normal",
    ) in {
        "history",
        "repeat",
    }:
        return plan

    q = question.lower()

    csv_docs = [
        d
        for d in documents
        if d["file_type"] in {
            "csv",
            "excel",
        }
    ]

    numeric_map = {
        "sum": "sum",
        "total": "sum",
        "average": "average",
        "avg": "average",
        "mean": "average",
        "minimum": "min",
        "min ": "min",
        "maximum": "max",
        "max ": "max",
        "rank": "rank",
        "sort": "sort",
        "filter": "filter",
        "percentage": "percentage_change",
        "percent": "percentage_change",
        "year-over-year": "yoy",
        "year over year": "yoy",
        "yoy": "yoy",
        "growth": "yoy",
    }

    if (
        csv_docs
        and any(
            key in q
            for key in numeric_map
        )
    ):
        op_name = next(
            numeric_map[key]
            for key in numeric_map
            if key in q
        )

        has_load = any(
            op.op == "load_csv"
            for op in plan.operations
        )

        has_metric_op = any(
            op.op in {
                "sum",
                "average",
                "min",
                "max",
                "count",
                "rank",
                "sort",
                "percentage_change",
                "yoy",
                "compare",
            }
            for op in plan.operations
        )

        if (
            not has_load
            and not has_metric_op
        ):
            hint = csv_docs[0][
                "filename"
            ]

            operations = [
                PlanOp(
                    op="load_csv",
                    filename_hint=hint,
                )
            ]

            if plan.metrics:
                operations.append(
                    PlanOp(
                        op=op_name,
                        column=plan.metrics[0],
                    )
                )
            else:
                operations.append(
                    PlanOp(
                        op=op_name,
                    )
                )

            plan.operations = operations

        if plan.intent in {
            Intent.FACTUAL_LOOKUP,
            Intent.FOLLOW_UP_QUESTION,
        }:
            mapping = {
                "sum": Intent.SUM,
                "average": Intent.AVERAGE,
                "min": Intent.MINIMUM,
                "max": Intent.MAXIMUM,
                "rank": Intent.RANKING,
                "sort": Intent.SORTING,
                "filter": Intent.FILTERING,
                "percentage_change": Intent.PERCENTAGE_CHANGE,
                "yoy": Intent.YEAR_OVER_YEAR_COMPARISON,
            }

            plan.intent = mapping.get(
                op_name,
                Intent.CSV_AGGREGATION,
            )

    if not plan.years:
        plan.years = _years_in_text(
            question
        )

    if not plan.operations:
        plan.operations = [
            PlanOp(
                op="retrieve"
            )
        ]

    return plan


def _looks_like_follow_up(
    question: str,
) -> bool:
    q = question.strip().lower()

    if any(
        token in q
        for token in (
            "previous year",
            "last year",
            "prior year",
            "the difference",
            "compare that",
        )
    ):
        return True

    if (
        len(q.split()) <= 10
        and q.startswith(
            (
                "what about",
                "how about",
                "and ",
                "same for",
                "now ",
                "and what",
            )
        )
    ):
        return True

    return bool(
        re.match(
            r"^(what about|how about)\b",
            q,
        )
    )


def _entities_in_text(
    text: str,
) -> list[str]:
    found: list[str] = []

    for name in (
        "Engineering",
        "Sales",
        "Marketing",
        "Finance",
        "HR",
        "Operations",
    ):
        if re.search(
            rf"\b{re.escape(name)}\b",
            text,
            re.I,
        ):
            found.append(name)

    return found


def _years_in_text(
    text: str,
) -> list[int]:
    return [
        int(year)
        for year in re.findall(
            r"\b(20\d{2}|19\d{2})\b",
            text,
        )
    ]
