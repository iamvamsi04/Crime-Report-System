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

IMPORTANT STRUCTURED-DATA RULES:

1. CSV and Excel files are structured tabular data.

2. For CSV/Excel numerical questions, use structured analysis operations.
   Do NOT use retrieve-only for a question that requires calculation
   from CSV/Excel rows.

3. The analysis engine executes these operations using DuckDB SQL.
   Do NOT assume Pandas is used for calculation.

4. For an exact numeric lookup in a CSV/Excel file, use:
   load_csv
   + filter operation(s)
   + an aggregation/value operation on the requested numeric column.

5. For example:

   Question:
   "How many units were sold on 1/10/2014 for the product Carretera?"

   If the dataset contains Product, Date, and Units Sold:
   load_csv
   filter Product = Carretera
   filter Date = 1/10/2014
   sum Units Sold

6. For:
   "What was the total revenue in 2013?"

   If the dataset contains Year and Revenue:
   load_csv
   filter Year = 2013
   sum Revenue

7. The requested metric must NEVER be used as the filter value.

   "units sold for Carretera"
   means:
   metric = Units Sold
   entity/filter value = Carretera

   It does NOT mean:
   filter Units Sold = Carretera.

8. Use column names from the supplied document catalog whenever possible.

9. Never invent documents, columns, years, entities, or values.

10. For dates, preserve the user's date value in the filter.

11. For a question asking:
    "how many units",
    "number of units",
    "units sold",
    identify the numeric Units Sold column if it exists.

    Do NOT use count unless the user is asking how many rows/records exist.

12. "total revenue", "total sales", etc. mean SUM of the corresponding
    numeric column.

13. "average revenue" means AVG/average of the Revenue column.

14. "minimum revenue" means MIN of the Revenue column.

15. "maximum revenue" means MAX of the Revenue column.

16. If the question contains a year and the dataset has a Year column,
    create a filter for that year when the question asks about that specific
    year.

17. If the question contains a product/entity and the dataset has a matching
    entity/product column, identify the entity and allow the analysis layer
    to filter it.

18. For ranking questions, use rank or sort and provide the requested
    metric/column.

19. For grouping questions such as:
    "sales by department"
    use groupby with the appropriate entity column and numeric value column.

20. For percentage-change questions, use percentage_change.

21. For year-over-year questions, use yoy.

22. For comparing two entities, use compare.

23. For questions involving multiple documents or both document evidence
    and CSV calculations, include retrieve as well as the appropriate
    structured-data operations.

24. Never perform arithmetic yourself. The DuckDB analysis layer calculates
    numerical results.

25. If the question is a normal document question, use retrieve.

26. If it is a follow-up, set is_follow_up true and resolve missing
    information using the conversation context.

27. document_hints must contain filenames from the supplied document catalog
    when a particular document is relevant.
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

            except (json.JSONDecodeError, TypeError):
                log.warning(
                    "invalid_csv_profile document_id=%s",
                    doc.get("id"),
                )

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
        (context.entities and new_entities)
        or context.last_answer == MISSING_ANSWER
    ):
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
    Normalize a Gemini-generated plan against the actual CSV/Excel schema.

    This function decides WHAT structured operation is required.

    The actual calculation is performed later by DuckDB in analyze.py.
    """

    csv_docs = [
        d
        for d in documents
        if d["file_type"] in {"csv", "excel"}
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

    if not columns:
        return plan

    q = question.casefold()

    # ---------------------------------------------------------
    # Resolve metric
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # Resolve years
    # ---------------------------------------------------------

    years = _years_in_text(question)

    if years:
        plan.years = years

    # ---------------------------------------------------------
    # Resolve operation type BEFORE constructing operations.
    #
    # This is important for YoY/percentage/ranking/grouping.
    # ---------------------------------------------------------

    operation_name = _determine_operation(
        question=question,
        plan=plan,
    )

    # ---------------------------------------------------------
    # Determine entity column
    # ---------------------------------------------------------

    entity_column = _find_entity_column(
        columns=columns,
        entity_columns=entity_columns,
        question=question,
    )

    # ---------------------------------------------------------
    # Build filters
    # ---------------------------------------------------------

    filters: list[PlanOp] = []

    # Year filter is appropriate for a single explicit year.
    #
    # Do NOT add this for a two-year comparison because the SQL
    # needs both years.
    if (
        len(plan.years) == 1
        and year_columns
        and operation_name
        not in {
            "percentage_change",
            "yoy",
        }
    ):
        filters.append(
            PlanOp(
                op="filter",
                column=year_columns[0],
                value=plan.years[0],
            )
        )

    # Entity filter.
    #
    # For compare, the SQL needs both entities, so don't reduce the
    # dataset to only one entity.
    if (
        len(plan.entities) == 1
        and entity_column
        and operation_name != "compare"
    ):
        filters.append(
            PlanOp(
                op="filter",
                column=entity_column,
                value=plan.entities[0],
            )
        )

    # Date filter.
    date_value = _extract_date_value(
        question
    )

    date_columns = _find_date_columns(
        columns
    )

    if date_value and date_columns:
        filters.append(
            PlanOp(
                op="filter",
                column=date_columns[0],
                value=date_value,
            )
        )

    # Preserve valid Gemini-generated filters.
    for existing in plan.operations:
        if existing.op != "filter":
            continue

        column = existing.column
        value = existing.value

        if not column or value is None:
            continue

        resolved = _resolve_column_name(
            columns,
            column,
        )

        if resolved is None:
            continue

        # Never allow a numeric metric to become an entity filter.
        if (
            resolved in numeric_columns
            and isinstance(value, str)
            and entity_column
            and _value_exists(
                profile=profile,
                column=entity_column,
                value=value,
            )
        ):
            continue

        filters.append(
            PlanOp(
                op="filter",
                column=resolved,
                value=value,
            )
        )

    filters = _deduplicate_filters(
        filters
    )

    # ---------------------------------------------------------
    # Construct operation chain
    # ---------------------------------------------------------

    if metric_column and _is_structured_numeric_question(
        question=question,
        metric_column=metric_column,
        columns=columns,
    ):
        operations: list[PlanOp] = [
            PlanOp(
                op="load_csv",
                filename_hint=doc["filename"],
            )
        ]

        operations.extend(filters)

        if operation_name in {
            "percentage_change",
            "yoy",
        }:
            operations.append(
                PlanOp(
                    op=operation_name,
                    column=metric_column,
                    value_column=metric_column,
                    from_year=(
                        min(plan.years)
                        if len(plan.years) >= 2
                        else None
                    ),
                    to_year=(
                        max(plan.years)
                        if len(plan.years) >= 2
                        else None
                    ),
                )
            )

        elif operation_name == "groupby":
            operations.append(
                PlanOp(
                    op="groupby",
                    column=(
                        entity_column
                        or (
                            entity_columns[0]
                            if entity_columns
                            else None
                        )
                    ),
                    value_column=metric_column,
                    agg="sum",
                )
            )

        elif operation_name == "compare":
            operations.append(
                PlanOp(
                    op="compare",
                    column=metric_column,
                    value_column=metric_column,
                    target=entity_column,
                )
            )

        elif operation_name in {
            "rank",
            "sort",
        }:
            operations.append(
                PlanOp(
                    op=operation_name,
                    column=metric_column,
                    value_column=metric_column,
                    n=10,
                    ascending=False,
                )
            )

        else:
            operations.append(
                PlanOp(
                    op=operation_name,
                    column=metric_column,
                    value_column=metric_column,
                )
            )

        # If this question also requires document evidence,
        # preserve retrieve alongside the structured analysis.
        if _requires_document_retrieval(
            plan
        ) and not any(
            op.op == "retrieve"
            for op in operations
        ):
            operations.insert(
                0,
                PlanOp(op="retrieve"),
            )

        plan.operations = operations

        plan.document_ids = [
            doc["id"]
        ]
        plan.document_hints = [
            doc["filename"]
        ]

        plan.intent = _intent_for_operation(
            operation_name
        )

        return plan

    return plan


def _determine_operation(
    *,
    question: str,
    plan: QueryPlan,
) -> str:
    q = question.casefold()

    # Explicit comparison first.
    if (
        len(plan.entities) >= 2
        and (
            "compare" in q
            or "difference between" in q
            or "versus" in q
            or " vs " in q
        )
    ):
        return "compare"

    # YoY must be checked before generic "growth".
    if (
        "year-over-year" in q
        or "year over year" in q
        or "yoy" in q
        or "previous year" in q
        or "last year" in q
        or "prior year" in q
    ):
        return "yoy"

    if (
        "percentage change" in q
        or "percent change" in q
        or "percentage increase" in q
        or "percentage decrease" in q
        or "percent increase" in q
        or "percent decrease" in q
    ):
        return "percentage_change"

    if (
        "group by" in q
        or "by department" in q
        or "by product" in q
        or "by company" in q
        or "by category" in q
        or "by division" in q
    ):
        return "groupby"

    if (
        "rank" in q
        or "top " in q
        or "highest" in q
        or "lowest" in q
    ):
        return "rank"

    if (
        "sort" in q
        or "sorted" in q
        or "order by" in q
    ):
        return "sort"

    if (
        "average" in q
        or "avg" in q
        or "mean" in q
    ):
        return "average"

    if (
        "minimum" in q
        or "lowest value" in q
        or "smallest value" in q
    ):
        return "min"

    if (
        "maximum" in q
        or "highest value" in q
        or "largest value" in q
    ):
        return "max"

    if (
        "how many rows" in q
        or "how many records" in q
        or "number of rows" in q
        or "number of records" in q
    ):
        return "count"

    # Default numeric lookup is SUM.
    return "sum"


def _requires_document_retrieval(
    plan: QueryPlan,
) -> bool:
    return any(
        op.op == "retrieve"
        for op in plan.operations
    )


def _intent_for_operation(
    operation: str,
) -> Intent:
    mapping = {
        "sum": Intent.SUM,
        "average": Intent.AVERAGE,
        "min": Intent.MINIMUM,
        "max": Intent.MAXIMUM,
        "count": Intent.NUMERICAL_LOOKUP,
        "rank": Intent.RANKING,
        "sort": Intent.SORTING,
        "filter": Intent.FILTERING,
        "groupby": Intent.CSV_AGGREGATION,
        "percentage_change": Intent.PERCENTAGE_CHANGE,
        "yoy": Intent.YEAR_OVER_YEAR_COMPARISON,
        "compare": Intent.ENTITY_COMPARISON,
    }

    return mapping.get(
        operation,
        Intent.CSV_AGGREGATION,
    )


def _is_structured_numeric_question(
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
        "growth",
        "percentage",
        "percent",
        "%",
        "rank",
        "top",
        "highest",
        "lowest",
        "compare",
        "difference",
        "group",
        "by ",
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
    # First trust an existing planner metric if it resolves to a real
    # numeric column.
    for metric in existing_metrics:
        resolved = _resolve_column_name(
            numeric_columns or columns,
            metric,
        )

        if (
            resolved
            and resolved in columns
            and (
                not numeric_columns
                or resolved in numeric_columns
            )
        ):
            return resolved

    q = question.casefold()

    candidates: list[
        tuple[int, str]
    ] = []

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

        if "revenue" in normalized and (
            "revenue" in q
            or "sales revenue" in q
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

    for column in entity_columns:
        normalized = column.casefold()

        if (
            "department" in normalized
            and "department" in q
        ):
            return column

    return entity_columns[0]


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

    if len(csv_docs) == 1:
        return csv_docs[0]

    return csv_docs[0] if csv_docs else None


def _get_profile(
    doc: dict[str, Any],
) -> dict[str, Any]:
    raw = doc.get("csv_profile")

    if not raw:
        return {}

    if isinstance(raw, dict):
        return raw

    try:
        return json.loads(raw)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return {}


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
    seen: set[
        tuple[str, str]
    ] = set()

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


def _find_date_columns(
    columns: list[str],
) -> list[str]:
    result: list[str] = []

    for column in columns:
        normalized = re.sub(
            r"[^a-z0-9]+",
            "_",
            column.casefold(),
        ).strip("_")

        if (
            normalized == "date"
            or normalized.endswith("_date")
            or normalized.startswith("date_")
            or "date" in normalized
        ):
            result.append(column)

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


def _value_exists(
    *,
    profile: dict[str, Any],
    column: str,
    value: Any,
) -> bool:
    """
    csv_profile intentionally does not store every dataset value.

    Therefore this function only handles values when a future profile
    explicitly provides sample/known values. It returns False otherwise,
    meaning the filter is preserved rather than discarded.
    """

    samples = profile.get(
        "sample_values"
    )

    if not isinstance(samples, dict):
        return False

    values = samples.get(column)

    if not isinstance(values, list):
        return False

    target = str(value).strip().casefold()

    return any(
        str(item).strip().casefold()
        == target
        for item in values
    )


def _apply_heuristics(
    plan: QueryPlan,
    question: str,
    documents: list[dict[str, Any]],
) -> QueryPlan:
    """
    Lightweight deterministic normalization.

    This function does not perform calculations.
    DuckDB performs all CSV/Excel calculations later.
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

    if not plan.years:
        plan.years = _years_in_text(
            question
        )

    if not plan.operations:
        plan.operations = [
            PlanOp(op="retrieve")
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
