from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.gemini import GeminiService
from app.models import Intent, PlanOp, QueryPlan

log = logging.getLogger(__name__)


PLAN_SYSTEM = """
You are the planning component of an intelligent document analysis system.

Your job is to understand the user's question and create a structured
execution plan.

The system supports:
- PDF and TXT document retrieval
- CSV and Excel structured-data analysis
- questions requiring both document evidence and structured-data calculations
- follow-up questions using conversation context
- multi-document questions

For CSV and Excel questions, DO NOT calculate values yourself.
DO NOT invent values.
DO NOT generate SQL.
Instead, identify the user's intended calculation and the relevant
dataset, columns, filters, entities, metrics, and years.

A later execution layer will use the stored CSV schema/profile and
generate DuckDB SQL.

Return JSON matching the QueryPlan model.

Important rules:

1. Never invent a document, column, entity, year, metric, or numeric value.

2. For CSV/Excel questions:
   - identify the relevant dataset if possible
   - identify the requested metric
   - identify filters
   - identify entities
   - identify years
   - identify the intended operation

3. Structured-data operations include:
   - sum
   - average
   - min
   - max
   - count
   - sort
   - rank
   - filter
   - groupby
   - percentage_change
   - yoy
   - compare

4. Use `load_csv` only to indicate that a CSV/Excel dataset is required.
   It does NOT mean Pandas should perform the calculation.

5. Do not perform arithmetic in this planning stage.

6. For a numerical CSV/Excel question, the operation chain should describe
   WHAT needs to be calculated, not HOW it is calculated internally.

7. For example:
   "What was the total profit in 2024?"
   should identify:
   - metric: profit
   - year: 2024
   - aggregation: sum

8. For:
   "What was the average sales price for Germany?"
   identify:
   - metric: sales price
   - entity/filter: Germany
   - aggregation: average

9. For:
   "Which department had the highest revenue?"
   identify:
   - entity/grouping column
   - revenue metric
   - ranking operation
   - descending order

10. For:
   "What was the percentage change in revenue between 2023 and 2024?"
   identify:
   - revenue metric
   - from year: 2023
   - to year: 2024
   - percentage_change operation

11. Questions can require both document retrieval and CSV analysis.
    Keep both requirements in the plan.

12. Follow-up questions must use the supplied conversation context.
    Resolve references such as:
    "that department", "the second one", "what about 2024?",
    "compare it with last year", etc.

13. If the question cannot be answered from the available documents,
    mark it appropriately as ambiguous, missing_information, or
    out_of_scope.

14. Do not assume a value merely because it sounds plausible.

Return only the JSON object.
"""


def build_plan(
    question: str,
    context: Any,
    documents: list[dict[str, Any]],
    gemini: GeminiService,
) -> QueryPlan:
    catalog = _document_catalog(documents)

    user_payload = {
        "question": question,
        "conversation_context": _context_payload(context),
        "documents": catalog,
    }

    raw = gemini.generate_json(
        PLAN_SYSTEM,
        json.dumps(user_payload, ensure_ascii=False),
    )

    plan = QueryPlan.model_validate(raw)

    plan = _merge_follow_up(
        question=question,
        plan=plan,
        context=context,
        documents=documents,
    )

    plan = _apply_csv_normalization(
        question=question,
        plan=plan,
        documents=documents,
    )

    plan = _apply_heuristics(
        question=question,
        plan=plan,
        documents=documents,
    )

    _filter_document_ids(plan, documents)

    return plan


def _document_catalog(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []

    for doc in documents:
        entry = {
            "id": doc.get("id"),
            "filename": doc.get("filename"),
            "file_type": doc.get("file_type"),
            "status": doc.get("status"),
        }

        if doc.get("file_type") in {"csv", "excel"}:
            profile = _get_profile(doc)

            if profile:
                entry["csv_profile"] = {
                    "columns": profile.get("columns", []),
                    "dtypes": profile.get("dtypes", {}),
                    "row_count": profile.get("row_count"),
                    "numeric_columns": profile.get("numeric_columns", []),
                    "year_columns": profile.get("year_columns", []),
                    "entity_columns": profile.get("entity_columns", []),
                    "categorical_columns": profile.get(
                        "categorical_columns",
                        [],
                    ),
                }

        catalog.append(entry)

    return catalog


def _context_payload(context: Any) -> dict[str, Any]:
    if context is None:
        return {}

    if hasattr(context, "model_dump"):
        return context.model_dump()

    if isinstance(context, dict):
        return context

    return {}


def _merge_follow_up(
    *,
    question: str,
    plan: QueryPlan,
    context: Any,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    if not _looks_like_follow_up(question):
        return plan

    context_data = _context_payload(context)

    previous_question = context_data.get("last_question")
    previous_plan = context_data.get("last_plan_summary")

    if not previous_question and not previous_plan:
        return plan

    plan.follow_up = True

    if not plan.resolved_question:
        if previous_question:
            plan.resolved_question = (
                f"{previous_question}. Follow-up: {question}"
            )
        else:
            plan.resolved_question = question

    previous_entities = context_data.get("entities") or []
    previous_metrics = context_data.get("metrics") or []
    previous_years = context_data.get("years") or []
    previous_document_ids = context_data.get("document_ids") or []

    if not plan.entities:
        plan.entities = list(previous_entities)

    if not plan.metrics:
        plan.metrics = list(previous_metrics)

    if not plan.years:
        plan.years = list(previous_years)

    if not plan.document_ids:
        plan.document_ids = list(previous_document_ids)

    if previous_plan and not plan.operations:
        try:
            previous = QueryPlan.model_validate(previous_plan)

            if previous.operations:
                plan.operations = [
                    PlanOp.model_validate(operation.model_dump())
                    for operation in previous.operations
                ]
        except Exception:
            log.debug("previous_plan_context_could_not_be_restored")

    return plan


def _apply_csv_normalization(
    *,
    question: str,
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    document = _choose_csv_document(plan, documents)

    if document is None:
        return plan

    profile = _get_profile(document)

    if not profile:
        return plan

    columns = [
        str(column)
        for column in profile.get("columns", [])
    ]

    numeric_columns = [
        str(column)
        for column in profile.get("numeric_columns", [])
    ]

    entity_columns = [
        str(column)
        for column in profile.get("entity_columns", [])
    ]

    year_columns = [
        str(column)
        for column in profile.get("year_columns", [])
    ]

    categorical_columns = [
        str(column)
        for column in profile.get("categorical_columns", [])
    ]

    plan.document_ids = list(
        dict.fromkeys(
            [*plan.document_ids, document["id"]]
        )
    )

    if not plan.document_hints:
        plan.document_hints = [document["filename"]]

    plan.filters = _normalize_filters(
        plan.filters,
        columns=columns,
        entity_columns=entity_columns,
        categorical_columns=categorical_columns,
    )

    plan.metrics = _normalize_metrics(
        plan.metrics,
        columns=columns,
        numeric_columns=numeric_columns,
    )

    plan.entities = _normalize_entities(
        plan.entities,
        entity_columns=entity_columns,
    )

    explicit_years = _years_in_text(question)

    if explicit_years:
        plan.years = list(
            dict.fromkeys(
                [*plan.years, *explicit_years]
            )
        )

    metric_column = _find_metric_column(
        question=question,
        plan=plan,
        numeric_columns=numeric_columns,
    )

    if metric_column and metric_column not in plan.metrics:
        plan.metrics.append(metric_column)

    if _is_csv_question(question, plan):
        plan = _normalize_csv_operations(
            question=question,
            plan=plan,
            metric_column=metric_column,
            numeric_columns=numeric_columns,
            year_columns=year_columns,
        )

    return plan


def _normalize_filters(
    filters: list[Any],
    *,
    columns: list[str],
    entity_columns: list[str],
    categorical_columns: list[str],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for item in filters:
        if not isinstance(item, dict):
            continue

        raw_column = (
            item.get("column")
            or item.get("field")
            or item.get("name")
        )

        if not raw_column:
            continue

        resolved = _resolve_column_name(
            str(raw_column),
            columns,
        )

        if resolved is None:
            continue

        value = item.get("value")

        result.append(
            {
                "column": resolved,
                "value": value,
            }
        )

    return _deduplicate_filters(result)


def _normalize_metrics(
    metrics: list[str],
    *,
    columns: list[str],
    numeric_columns: list[str],
) -> list[str]:
    normalized: list[str] = []

    for metric in metrics:
        resolved = _resolve_column_name(
            metric,
            numeric_columns or columns,
        )

        if resolved and resolved not in normalized:
            normalized.append(resolved)

    return normalized


def _normalize_entities(
    entities: list[str],
    *,
    entity_columns: list[str],
) -> list[str]:
    if not entities:
        return []

    return [
        entity.strip()
        for entity in entities
        if isinstance(entity, str) and entity.strip()
    ]


def _normalize_csv_operations(
    *,
    question: str,
    plan: QueryPlan,
    metric_column: str | None,
    numeric_columns: list[str],
    year_columns: list[str],
) -> QueryPlan:
    operations = list(plan.operations)

    if not operations:
        operations.append(
            PlanOp(op="load_csv")
        )

    requested = _requested_aggregation(question, plan)

    if requested:
        if not any(
            operation.op in {
                "sum",
                "average",
                "min",
                "max",
                "count",
            }
            for operation in operations
        ):
            operations.append(
                PlanOp(
                    op=requested,
                    column=metric_column,
                    value_column=metric_column,
                )
            )

    elif _contains_keyword(question, "rank", "highest", "lowest", "top", "bottom"):
        if not any(
            operation.op in {"rank", "sort"}
            for operation in operations
        ):
            operations.append(
                PlanOp(
                    op="rank",
                    column=metric_column,
                    value_column=metric_column,
                    ascending=_contains_keyword(
                        question,
                        "lowest",
                        "bottom",
                    ),
                    n=10,
                )
            )

    elif _contains_keyword(
        question,
        "percentage change",
        "percent change",
        "growth",
    ):
        if not any(
            operation.op == "percentage_change"
            for operation in operations
        ):
            operations.append(
                PlanOp(
                    op="percentage_change",
                    column=metric_column,
                    value_column=metric_column,
                    year_column=year_columns[0] if year_columns else None,
                    **_year_range_kwargs(plan.years),
                )
            )

    elif _contains_keyword(
        question,
        "year over year",
        "year-over-year",
        "yoy",
    ):
        if not any(
            operation.op == "yoy"
            for operation in operations
        ):
            operations.append(
                PlanOp(
                    op="yoy",
                    column=metric_column,
                    value_column=metric_column,
                    year_column=year_columns[0] if year_columns else None,
                    **_year_range_kwargs(plan.years),
                )
            )

    elif _contains_keyword(question, "compare", "comparison"):
        if not any(
            operation.op == "compare"
            for operation in operations
        ):
            operations.append(
                PlanOp(
                    op="compare",
                    column=metric_column,
                    value_column=metric_column,
                )
            )

    plan.operations = _deduplicate_operations(operations)

    if plan.intent in {
        Intent.FACTUAL_LOOKUP,
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
    }:
        return plan

    if requested == "sum":
        plan.intent = Intent.SUM
    elif requested == "average":
        plan.intent = Intent.AVERAGE
    elif requested == "min":
        plan.intent = Intent.MINIMUM
    elif requested == "max":
        plan.intent = Intent.MAXIMUM
    elif _contains_keyword(question, "rank", "highest", "lowest", "top", "bottom"):
        plan.intent = Intent.RANKING
    elif _contains_keyword(
        question,
        "percentage change",
        "percent change",
        "growth",
    ):
        plan.intent = Intent.PERCENTAGE_CHANGE
    elif _contains_keyword(
        question,
        "year over year",
        "year-over-year",
        "yoy",
    ):
        plan.intent = Intent.YEAR_OVER_YEAR_COMPARISON
    elif _contains_keyword(question, "compare", "comparison"):
        plan.intent = Intent.ENTITY_COMPARISON
    else:
        plan.intent = Intent.CSV_AGGREGATION

    return plan


def _requested_aggregation(
    question: str,
    plan: QueryPlan,
) -> str | None:
    if plan.operations:
        for operation in plan.operations:
            if operation.op in {
                "sum",
                "average",
                "min",
                "max",
                "count",
            }:
                return operation.op

    lowered = question.lower()

    if _contains_keyword(
        lowered,
        "average",
        "avg",
        "mean",
    ):
        return "average"

    if _contains_keyword(
        lowered,
        "minimum",
        "minimum value",
        "lowest value",
        "smallest",
        "min",
    ):
        return "min"

    if _contains_keyword(
        lowered,
        "maximum",
        "maximum value",
        "highest value",
        "largest",
        "max",
    ):
        return "max"

    if _contains_keyword(
        lowered,
        "count",
        "number of",
        "how many",
    ):
        return "count"

    if _contains_keyword(
        lowered,
        "total",
        "sum",
    ):
        return "sum"

    return None


def _find_metric_column(
    *,
    question: str,
    plan: QueryPlan,
    numeric_columns: list[str],
) -> str | None:
    for metric in plan.metrics:
        resolved = _resolve_column_name(
            metric,
            numeric_columns,
        )

        if resolved:
            return resolved

    if not numeric_columns:
        return None

    lowered = question.lower()

    aliases = {
        "revenue": [
            "revenue",
            "sales",
            "gross sales",
            "net sales",
        ],
        "profit": [
            "profit",
            "profit amount",
        ],
        "cost": [
            "cost",
            "cogs",
            "cost of goods sold",
        ],
        "units": [
            "units",
            "units sold",
            "quantity",
        ],
        "price": [
            "price",
            "sales price",
            "unit price",
        ],
    }

    for aliases_for_metric in aliases.values():
        for alias in aliases_for_metric:
            if alias in lowered:
                resolved = _resolve_column_name(
                    alias,
                    numeric_columns,
                )

                if resolved:
                    return resolved

    for column in numeric_columns:
        normalized = _normalize_text(column)

        if normalized and normalized in _normalize_text(question):
            return column

    return None


def _choose_csv_document(
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> dict[str, Any] | None:
    csv_documents = [
        doc
        for doc in documents
        if doc.get("status") == "ready"
        and doc.get("file_type") in {"csv", "excel"}
    ]

    if not csv_documents:
        return None

    if plan.document_ids:
        for document_id in plan.document_ids:
            for document in csv_documents:
                if document.get("id") == document_id:
                    return document

    if plan.document_hints:
        hints = [
            hint.lower()
            for hint in plan.document_hints
        ]

        for document in csv_documents:
            filename = document.get("filename", "").lower()

            if any(
                hint in filename or filename in hint
                for hint in hints
            ):
                return document

    return csv_documents[0]


def _get_profile(document: dict[str, Any]) -> dict[str, Any] | None:
    raw = document.get("csv_profile")

    if isinstance(raw, dict):
        return raw

    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)

            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            log.warning(
                "invalid_csv_profile document=%s",
                document.get("id"),
            )

    return None


def _resolve_column_name(
    requested: str,
    columns: list[str],
) -> str | None:
    requested_normalized = _normalize_text(requested)

    if not requested_normalized:
        return None

    for column in columns:
        if _normalize_text(column) == requested_normalized:
            return column

    for column in columns:
        normalized = _normalize_text(column)

        if (
            requested_normalized in normalized
            or normalized in requested_normalized
        ):
            return column

    return None


def _normalize_text(value: Any) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value).lower(),
    ).strip()


def _deduplicate_filters(
    filters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in filters:
        key = json.dumps(
            item,
            sort_keys=True,
            default=str,
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def _deduplicate_operations(
    operations: list[PlanOp],
) -> list[PlanOp]:
    result: list[PlanOp] = []
    seen: set[str] = set()

    for operation in operations:
        key = json.dumps(
            operation.model_dump(exclude_none=True),
            sort_keys=True,
            default=str,
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(operation)

    return result


def _year_range_kwargs(
    years: list[int],
) -> dict[str, int]:
    if len(years) < 2:
        return {}

    return {
        "from_year": years[-2],
        "to_year": years[-1],
    }


def _is_csv_question(
    question: str,
    plan: QueryPlan,
) -> bool:
    csv_intents = {
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
        Intent.NUMERICAL_LOOKUP,
    }

    if plan.intent in csv_intents:
        return True

    if any(
        operation.op != "retrieve"
        for operation in plan.operations
    ):
        return True

    lowered = question.lower()

    return _contains_keyword(
        lowered,
        "total",
        "sum",
        "average",
        "avg",
        "mean",
        "highest",
        "lowest",
        "maximum",
        "minimum",
        "count",
        "how many",
        "ranking",
        "rank",
        "growth",
        "percentage change",
        "year over year",
        "compare",
    )


def _apply_heuristics(
    *,
    question: str,
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    if plan.out_of_scope or plan.ambiguous:
        return plan

    lowered = question.lower()

    has_dataset = _choose_csv_document(
        plan,
        documents,
    ) is not None

    if not has_dataset:
        return plan

    if _contains_keyword(
        lowered,
        "total",
        "sum",
        "average",
        "avg",
        "mean",
        "highest",
        "lowest",
        "maximum",
        "minimum",
        "count",
        "how many",
        "ranking",
        "rank",
        "percentage change",
        "percent change",
        "year over year",
        "year-over-year",
        "yoy",
        "compare",
    ):
        if not plan.operations:
            plan.operations = [
                PlanOp(op="load_csv")
            ]

    return plan


def _filter_document_ids(
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> None:
    valid_ids = {
        str(document.get("id"))
        for document in documents
        if document.get("status") == "ready"
    }

    plan.document_ids = [
        document_id
        for document_id in plan.document_ids
        if document_id in valid_ids
    ]


def _contains_keyword(
    text: str,
    *keywords: str,
) -> bool:
    lowered = text.lower()

    return any(
        keyword.lower() in lowered
        for keyword in keywords
    )


def _years_in_text(question: str) -> list[int]:
    years = re.findall(
        r"\b(?:19|20)\d{2}\b",
        question,
    )

    return list(
        dict.fromkeys(
            int(year)
            for year in years
        )
    )


def _looks_like_follow_up(question: str) -> bool:
    lowered = question.lower().strip()

    if not lowered:
        return False

    short_follow_ups = {
        "what about 2024",
        "what about 2023",
        "and 2024",
        "and 2023",
        "compare that",
        "compare it",
        "what about it",
        "the second one",
        "the first one",
        "same for",
        "how about",
        "what about",
    }

    if lowered in short_follow_ups:
        return True

    if len(lowered.split()) <= 8 and any(
        phrase in lowered
        for phrase in (
            "what about",
            "how about",
            "and ",
            "same ",
            "compare it",
            "compare that",
        )
    ):
        return True

    return False

