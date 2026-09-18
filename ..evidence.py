from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from typing import Any

from app.models import (
    AnalysisResult,
    ConflictItem,
    ConflictReport,
    Evidence,
    ExecutionMode,
    NumericClaim,
    QueryPlan,
    Source,
)

log = logging.getLogger(__name__)


MISSING_ANSWER = (
    "The requested information was not found in the available documents."
)

OUT_OF_SCOPE_ANSWER = (
    "The requested information is outside the available document evidence."
)


# ---------------------------------------------------------------------------
# Evidence validation
# ---------------------------------------------------------------------------


def validate_evidence(
    *,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> bool:
    """
    Determine whether the required evidence path produced usable evidence.

    RETRIEVAL:
        requires retrieved document evidence.

    STRUCTURED:
        requires at least one successfully executed structured result.

    HYBRID:
        requires BOTH.

    A structured query returning zero rows is still valid evidence. It may
    correctly establish that no records matched the requested conditions.
    """

    has_retrieval = bool(evidence)
    has_structured = _has_verified_analysis(
        analysis
    )

    if plan.mode == ExecutionMode.RETRIEVAL:
        return has_retrieval

    if plan.mode == ExecutionMode.STRUCTURED:
        return has_structured

    if plan.mode == ExecutionMode.HYBRID:
        return (
            has_retrieval
            and has_structured
        )

    return False


def _has_verified_analysis(
    analysis: list[AnalysisResult],
) -> bool:
    for result in analysis:
        # Dynamic structured results are produced only after SQL validation
        # and successful DuckDB execution.
        if result.operation == "dynamic_sql":
            return True

        # Keep this generic in case future verified executors are added.
        if (
            result.query
            or result.value is not None
            or result.table
            or result.rows_used == 0
        ):
            return True

    return False


# ---------------------------------------------------------------------------
# Source construction
# ---------------------------------------------------------------------------


def sources_from_evidence(
    *,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> list[Source]:
    """
    Convert retrieved and computed evidence into frontend source objects.

    Structured results point back to the uploaded source files while also
    recording that the displayed answer came from verified computation.
    """

    sources: list[Source] = []
    seen: set[tuple[Any, ...]] = set()

    for item in evidence:
        key = (
            "retrieval",
            item.document_id,
            item.chunk_id,
            item.source_reference,
        )

        if key in seen:
            continue

        seen.add(key)

        sources.append(
            Source(
                document_id=item.document_id,
                filename=item.filename,
                source_reference=(
                    item.source_reference
                    or _retrieval_reference(item)
                ),
                excerpt=_excerpt(item.text),
                document_type=item.document_type,
                page_number=item.page_number,
                section=item.section,
                row_start=item.row_start,
                row_end=item.row_end,
            )
        )

    for result in analysis:
        filenames = list(
            result.source_filenames
        )

        document_ids = list(
            result.source_document_ids
        )

        # Compatibility with a single-source AnalysisResult.
        if not filenames and result.filename:
            filenames = [
                result.filename
            ]

        if (
            not document_ids
            and result.document_id
        ):
            document_ids = [
                result.document_id
            ]

        source_count = max(
            len(filenames),
            len(document_ids),
            1,
        )

        for index in range(source_count):
            filename = (
                filenames[index]
                if index < len(filenames)
                else ""
            )

            document_id = (
                document_ids[index]
                if index < len(document_ids)
                else ""
            )

            key = (
                "analysis",
                document_id,
                filename,
                result.query or "",
            )

            if key in seen:
                continue

            seen.add(key)

            sources.append(
                Source(
                    document_id=document_id,
                    filename=filename,
                    source_reference=(
                        _analysis_reference(
                            filename=filename,
                            result=result,
                        )
                    ),
                    excerpt=_analysis_excerpt(
                        result
                    ),
                    document_type=_structured_type(
                        filename
                    ),
                    operation=result.operation,
                    formula=(
                        result.formula
                        or result.query
                    ),
                    value=result.value,
                )
            )

    return sources


def _retrieval_reference(
    item: Evidence,
) -> str:
    if item.page_number:
        return (
            f"{item.filename}, "
            f"page {item.page_number}"
        )

    if (
        item.start_line
        and item.end_line
    ):
        return (
            f"{item.filename}, "
            f"lines {item.start_line}-{item.end_line}"
        )

    if item.section:
        return (
            f"{item.filename}, "
            f"{item.section}"
        )

    return item.filename


def _analysis_reference(
    *,
    filename: str,
    result: AnalysisResult,
) -> str:
    base = (
        filename
        or "structured dataset"
    )

    if result.value is not None:
        return (
            f"{base} — verified structured analysis"
        )

    if result.rows_used is not None:
        return (
            f"{base} — verified structured analysis "
            f"({result.rows_used} result row(s))"
        )

    return (
        f"{base} — verified structured analysis"
    )


def _analysis_excerpt(
    result: AnalysisResult,
) -> str:
    """
    Give the existing Streamlit source display something meaningful without
    dumping the entire result or SQL query into the UI.
    """

    if result.value is not None:
        return (
            "Computed result: "
            f"{_display_value(result.value)}"
        )

    if result.table:
        preview = result.table[:3]

        text = "; ".join(
            _row_preview(row)
            for row in preview
        )

        if result.truncated:
            text += "; result truncated"

        return _excerpt(
            text,
            limit=400,
        )

    if result.rows_used == 0:
        return (
            "The verified structured query returned no matching rows."
        )

    if result.explanation:
        return _excerpt(
            result.explanation,
            limit=400,
        )

    return "Verified structured analysis."


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def detect_conflicts(
    *,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> ConflictReport:
    """
    Detect clear numeric disagreements across different uploaded sources.

    Conflict detection is intentionally conservative.

    Dynamic SQL may produce arbitrary output columns, so we no longer pretend
    that the operation name itself is the business metric. Instead:

    - textual evidence contributes recognizable numeric claims;
    - scalar structured results contribute claims using their result column;
    - simple tabular structured results contribute numeric cells where a
      meaningful metric column can be identified.

    A conflict is reported only when:
    - the normalized metric/entity/year/unit keys match,
    - values differ materially,
    - and the claims come from different source files/documents.
    """

    claims: list[NumericClaim] = []

    for item in evidence:
        claims.extend(
            _claims_from_text(item)
        )

    for result in analysis:
        claims.extend(
            _claims_from_analysis(
                result
            )
        )

    grouped: dict[
        tuple[str, str, int, str],
        list[NumericClaim],
    ] = defaultdict(list)

    for claim in claims:
        key = (
            _normalize_label(
                claim.metric
            ),
            _normalize_label(
                claim.entity
            ),
            int(claim.year or 0),
            _normalize_label(
                claim.unit
            ),
        )

        grouped[key].append(claim)

    conflicts: list[ConflictItem] = []

    for (
        metric,
        entity,
        year,
        unit,
    ), group in grouped.items():
        if len(group) < 2:
            continue

        distinct_sources = {
            (
                claim.document_id,
                claim.filename.casefold(),
            )
            for claim in group
        }

        if len(distinct_sources) < 2:
            continue

        values = [
            claim.value
            for claim in group
            if math.isfinite(
                claim.value
            )
        ]

        if len(values) < 2:
            continue

        if not _values_conflict(values):
            continue

        conflicts.append(
            ConflictItem(
                metric=metric,
                entity=entity,
                year=year,
                unit=unit,
                claims=group,
            )
        )

    return ConflictReport(
        has_conflicts=bool(conflicts),
        conflicts=conflicts,
    )


# ---------------------------------------------------------------------------
# Structured numeric claims
# ---------------------------------------------------------------------------


def _claims_from_analysis(
    result: AnalysisResult,
) -> list[NumericClaim]:
    claims: list[NumericClaim] = []

    filenames = (
        result.source_filenames
        or (
            [result.filename]
            if result.filename
            else []
        )
    )

    document_ids = (
        result.source_document_ids
        or (
            [result.document_id]
            if result.document_id
            else []
        )
    )

    # A multi-source computed value is derived from several datasets rather
    # than independently asserted by each one. Treat it as one combined source
    # so it does not create a fake conflict against itself.
    filename = (
        filenames[0]
        if len(filenames) == 1
        else ", ".join(filenames)
    )

    document_id = (
        document_ids[0]
        if len(document_ids) == 1
        else "|".join(document_ids)
    )

    if _is_number(result.value):
        metric = (
            result.columns[0]
            if len(result.columns) == 1
            else "structured_result"
        )

        claims.append(
            NumericClaim(
                metric=metric,
                value=float(result.value),
                filename=filename,
                document_id=document_id,
                source_reference=(
                    _analysis_reference(
                        filename=filename,
                        result=result,
                    )
                ),
            )
        )

        return claims

    # For arbitrary tables, avoid aggressively interpreting every numeric cell
    # as a claim. We only extract straightforward result columns.
    if not result.table:
        return claims

    numeric_columns = _numeric_result_columns(
        result.table
    )

    if not numeric_columns:
        return claims

    dimension_columns = [
        column
        for column in result.columns
        if column not in numeric_columns
    ]

    for row in result.table:
        entity = _row_entity(
            row=row,
            dimension_columns=dimension_columns,
        )

        year = _row_year(
            row=row,
            dimension_columns=dimension_columns,
        )

        for column in numeric_columns:
            value = row.get(column)

            if not _is_number(value):
                continue

            # Avoid turning a dimension-like year column into a metric claim.
            if _looks_like_year_column(column):
                continue

            claims.append(
                NumericClaim(
                    metric=column,
                    value=float(value),
                    entity=entity,
                    year=year,
                    filename=filename,
                    document_id=document_id,
                    source_reference=(
                        _analysis_reference(
                            filename=filename,
                            result=result,
                        )
                    ),
                )
            )

    return claims


def _numeric_result_columns(
    rows: list[dict[str, Any]],
) -> list[str]:
    if not rows:
        return []

    columns: list[str] = []

    for column in rows[0]:
        values = [
            row.get(column)
            for row in rows
            if row.get(column) is not None
        ]

        if not values:
            continue

        if all(
            _is_number(value)
            for value in values
        ):
            columns.append(column)

    return columns


def _row_entity(
    *,
    row: dict[str, Any],
    dimension_columns: list[str],
) -> str:
    """
    Use the first non-numeric, non-year dimension as an entity label.

    Unlike the old implementation, this is not hard-coded to Engineering,
    Sales, Marketing, etc.
    """

    for column in dimension_columns:
        value = row.get(column)

        if value is None:
            continue

        if _looks_like_year_column(
            column
        ):
            continue

        if isinstance(value, str):
            value = value.strip()

            if value:
                return value

    return ""


def _row_year(
    *,
    row: dict[str, Any],
    dimension_columns: list[str],
) -> int:
    for column in dimension_columns:
        if not _looks_like_year_column(
            column
        ):
            continue

        value = row.get(column)

        year = _coerce_year(value)

        if year:
            return year

    return 0


# ---------------------------------------------------------------------------
# Text numeric claims
# ---------------------------------------------------------------------------


NUMBER_RE = re.compile(
    r"""
    (?<![\w])
    (?P<currency>[$€£₹])?
    (?P<number>
        [-+]?
        (?:
            \d{1,3}(?:,\d{3})+
            |
            \d+
        )
        (?:\.\d+)?
    )
    \s*
    (?P<suffix>%|percent|percentage)?
    """,
    re.IGNORECASE | re.VERBOSE,
)

YEAR_RE = re.compile(
    r"\b(19\d{2}|20\d{2}|21\d{2})\b"
)

METRIC_PATTERNS = (
    "revenue",
    "profit",
    "income",
    "sales",
    "salary",
    "cost",
    "expense",
    "amount",
    "total",
    "margin",
    "growth",
    "rate",
    "percentage",
    "percent",
    "headcount",
    "employees",
    "customers",
    "orders",
)


def _claims_from_text(
    item: Evidence,
) -> list[NumericClaim]:
    """
    Extract only simple, recognizable numeric claims from retrieved prose.

    This is not used to answer questions. It is only a best-effort conflict
    detector.
    """

    text = item.text or ""

    if not text:
        return []

    claims: list[NumericClaim] = []

    lowered = text.casefold()

    metric = _metric_from_text(
        lowered
    )

    if not metric:
        return []

    year = _first_year(text)

    for match in NUMBER_RE.finditer(text):
        raw_number = match.group(
            "number"
        )

        try:
            value = float(
                raw_number.replace(",", "")
            )
        except ValueError:
            continue

        # Avoid treating the detected year itself as the metric value.
        if (
            year
            and value == float(year)
            and match.group("currency") is None
            and match.group("suffix") is None
        ):
            continue

        unit = _number_unit(match)

        claims.append(
            NumericClaim(
                metric=metric,
                value=value,
                year=year,
                unit=unit,
                filename=item.filename,
                document_id=item.document_id,
                source_reference=(
                    item.source_reference
                    or _retrieval_reference(item)
                ),
            )
        )

    return claims


def _metric_from_text(
    lowered_text: str,
) -> str:
    for metric in METRIC_PATTERNS:
        if re.search(
            rf"\b{re.escape(metric)}\b",
            lowered_text,
        ):
            return metric

    return ""


def _first_year(text: str) -> int:
    match = YEAR_RE.search(text)

    if not match:
        return 0

    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _number_unit(
    match: re.Match[str],
) -> str:
    suffix = (
        match.group("suffix")
        or ""
    ).casefold()

    currency = (
        match.group("currency")
        or ""
    )

    if suffix in {
        "%",
        "percent",
        "percentage",
    }:
        return "%"

    if currency:
        return currency

    return ""


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def _values_conflict(
    values: list[float],
) -> bool:
    """
    Ignore tiny floating-point differences.

    Values materially conflict if their range exceeds both an absolute and
    relative tolerance.
    """

    minimum = min(values)
    maximum = max(values)

    difference = abs(
        maximum - minimum
    )

    scale = max(
        abs(minimum),
        abs(maximum),
        1.0,
    )

    tolerance = max(
        1e-9,
        scale * 1e-6,
    )

    return difference > tolerance


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False

    if not isinstance(
        value,
        (int, float),
    ):
        return False

    try:
        return math.isfinite(
            float(value)
        )
    except (TypeError, ValueError):
        return False


def _looks_like_year_column(
    column: str,
) -> bool:
    normalized = re.sub(
        r"[^a-z0-9]+",
        "",
        column.casefold(),
    )

    return normalized in {
        "year",
        "yr",
        "fiscalyear",
        "financialyear",
        "calendaryear",
    }


def _coerce_year(value: Any) -> int:
    if value is None:
        return 0

    if isinstance(value, bool):
        return 0

    if isinstance(value, int):
        return (
            value
            if 1000 <= value <= 9999
            else 0
        )

    if isinstance(value, float):
        if value.is_integer():
            year = int(value)

            return (
                year
                if 1000 <= year <= 9999
                else 0
            )

        return 0

    match = YEAR_RE.search(
        str(value)
    )

    if not match:
        return 0

    return int(match.group(1))


def _normalize_label(
    value: str,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(value or "")
        .strip()
        .casefold(),
    )


def _excerpt(
    text: str,
    limit: int = 500,
) -> str:
    text = re.sub(
        r"\s+",
        " ",
        str(text or ""),
    ).strip()

    if len(text) <= limit:
        return text

    return text[:limit] + "…"


def _row_preview(
    row: dict[str, Any],
) -> str:
    parts = [
        f"{key}={_display_value(value)}"
        for key, value in row.items()
    ]

    return ", ".join(parts)


def _display_value(
    value: Any,
) -> str:
    if value is None:
        return "NULL"

    if isinstance(value, float):
        return f"{value:g}"

    return str(value)


def _structured_type(
    filename: str,
) -> str:
    lowered = filename.casefold()

    if lowered.endswith(".csv"):
        return "csv"

    if lowered.endswith(
        (".xlsx", ".xls")
    ):
        return "excel"

    return "structured"
