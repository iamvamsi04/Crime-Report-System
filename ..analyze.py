from __future__ import annotations

import json
import logging
from typing import Any

import pandas as pd

from app.errors import AnalysisError, NotFoundError
from app.models import AnalysisResult, PlanOp, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)

ANALYSIS_OPS = {
    "load_csv",
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


def needs_analysis(plan: QueryPlan) -> bool:
    return any(
        op.op in ANALYSIS_OPS and op.op != "retrieve"
        for op in plan.operations
    )


def run_analysis(
    plan: QueryPlan,
    store: Storage,
) -> list[AnalysisResult]:
    frame: pd.DataFrame | None = None
    source_file: str | None = None
    results: list[AnalysisResult] = []

    for op in plan.operations:
        if op.op == "retrieve":
            continue

        if op.op == "load_csv":
            frame, source_file = _load_csv(
                store,
                op,
                plan,
            )

            results.append(
                AnalysisResult(
                    operation="load_csv",
                    source_file=source_file,
                    rows_used=len(frame),
                    columns_used=list(frame.columns),
                    inputs={"filename": source_file},
                )
            )
            continue

        if frame is None:
            csv_docs = [
                d
                for d in store.list_documents(ready_only=True)
                if d["file_type"] in {"csv", "excel"}
            ]

            if not csv_docs:
                raise AnalysisError(
                    "No CSV or Excel dataset is available for this calculation."
                )

            frame, source_file = _load_csv(
                store,
                PlanOp(
                    op="load_csv",
                    filename_hint=(
                        csv_docs[0]["filename"]
                    ),
                ),
                plan,
            )

        frame, result = _apply_op(
            frame,
            op,
            plan,
            source_file or "",
        )

        results.append(result)

        log.info(
            "csv_analysis op=%s file=%s",
            op.op,
            source_file,
        )

    return results


def _load_csv(
    store: Storage,
    op: PlanOp,
    plan: QueryPlan,
) -> tuple[pd.DataFrame, str]:
    docs = [
        d
        for d in store.list_documents(ready_only=True)
        if d["file_type"] in {"csv", "excel"}
    ]

    if not docs:
        raise AnalysisError(
            "No CSV or Excel dataset is available for this calculation."
        )

    hint = (
        op.filename_hint
        or (
            plan.document_hints[0]
            if plan.document_hints
            else ""
        )
    ).lower()

    chosen = docs[0]

    for doc in docs:
        if hint and hint in doc["filename"].lower():
            chosen = doc
            break

        if doc["id"] in plan.document_ids:
            chosen = doc
            break

    try:
        path = store.document_file(chosen["id"])

        if chosen["file_type"] == "excel":
            frame = pd.read_excel(
                path,
                sheet_name=0,
            )
        else:
            frame = pd.read_csv(path)

    except NotFoundError:
        raise

    except Exception as exc:
        raise AnalysisError(
            "The CSV or Excel dataset could not be loaded."
        ) from exc

    frame.columns = [
        str(column).strip()
        for column in frame.columns
    ]

    return frame, chosen["filename"]


def _apply_op(
    frame: pd.DataFrame,
    op: PlanOp,
    plan: QueryPlan,
    source_file: str,
) -> tuple[pd.DataFrame, AnalysisResult]:

    # ---------------------------------------------------------
    # FILTER
    # ---------------------------------------------------------

    if op.op == "filter":
        column = _resolve_filter_column(
            frame,
            op,
            plan,
        )

        value = _resolve_filter_value(
            frame,
            column,
            op,
            plan,
        )

        working = _filter_frame(
            frame,
            column,
            value,
        )

        if working.empty:
            raise AnalysisError(
                f"No rows matched {column}={value}."
            )

        return working, AnalysisResult(
            operation="filter",
            source_file=source_file,
            rows_used=len(working),
            columns_used=[column],
            inputs={
                "column": column,
                "value": value,
            },
        )

    # ---------------------------------------------------------
    # AGGREGATIONS
    # ---------------------------------------------------------

    if op.op in {
        "sum",
        "average",
        "min",
        "max",
        "count",
    }:
        filtered = _apply_plan_filters(
            frame,
            plan,
        )

        if filtered.empty:
            raise AnalysisError(
                "No rows matched the requested filters."
            )

        if op.op == "count":
            column = ""
            series = None
        else:
            column = _require_column(
                filtered,
                op.column or op.value_column,
                plan,
            )

            series = pd.to_numeric(
                filtered[column],
                errors="coerce",
            )

            if series.dropna().empty:
                raise AnalysisError(
                    f"Column '{column}' has no numeric values."
                )

        if op.op == "sum":
            value = float(series.sum())
            formula = f"sum({column})"

        elif op.op == "average":
            value = float(series.mean())
            formula = f"mean({column})"

        elif op.op == "min":
            value = float(series.min())
            formula = f"min({column})"

        elif op.op == "max":
            value = float(series.max())
            formula = f"max({column})"

        else:
            value = int(len(filtered))
            formula = "count(rows)"

        return filtered, AnalysisResult(
            operation=op.op,
            value=_clean_number(value),
            formula=formula,
            source_file=source_file,
            rows_used=len(filtered),
            columns_used=[column] if column else [],
            inputs={
                "column": column,
            },
        )

    # ---------------------------------------------------------
    # SORT / RANK
    # ---------------------------------------------------------

    if op.op in {"sort", "rank"}:
        working = _apply_plan_filters(
            frame,
            plan,
        )

        column = _require_column(
            working,
            op.column or op.value_column,
            plan,
        )

        n = op.n or 10

        numeric = pd.to_numeric(
            working[column],
            errors="coerce",
        )

        working = (
            working
            .assign(_sort=numeric)
            .sort_values(
                "_sort",
                ascending=op.ascending,
                na_position="last",
            )
            .drop(columns=["_sort"])
        )

        table = json.loads(
            working
            .head(n)
            .to_json(orient="records")
        )

        return working, AnalysisResult(
            operation=op.op,
            table=table,
            source_file=source_file,
            rows_used=len(working.head(n)),
            columns_used=list(working.columns),
            inputs={
                "column": column,
                "n": n,
                "ascending": op.ascending,
            },
        )

    # ---------------------------------------------------------
    # GROUP BY
    # ---------------------------------------------------------

    if op.op == "groupby":
        working = _apply_plan_filters(
            frame,
            plan,
        )

        column = _require_column(
            working,
            op.column,
            plan,
        )

        value_column = _require_column(
            working,
            op.value_column
            or (
                plan.metrics[0]
                if plan.metrics
                else None
            ),
            plan,
        )

        agg = (
            op.agg or "sum"
        ).lower()

        if agg not in {
            "sum",
            "mean",
            "min",
            "max",
            "count",
        }:
            raise AnalysisError(
                "Unsupported aggregation."
            )

        grouped = working.groupby(
            column,
            dropna=False,
        )[value_column]

        if agg == "count":
            out = grouped.count().reset_index(
                name=value_column
            )
        else:
            out = getattr(grouped, agg)().reset_index()

        table = json.loads(
            out.to_json(
                orient="records"
            )
        )

        return working, AnalysisResult(
            operation="groupby",
            table=table,
            source_file=source_file,
            rows_used=len(out),
            columns_used=[
                column,
                value_column,
            ],
            inputs={
                "by": column,
                "agg": agg,
                "value_column": value_column,
            },
        )

    # ---------------------------------------------------------
    # PERCENTAGE CHANGE / YOY
    # ---------------------------------------------------------

    if op.op in {
        "percentage_change",
        "yoy",
    }:
        filtered = _apply_plan_filters(
            frame,
            plan,
        )

        year_col = _year_column(
            filtered,
            op,
        )

        value_col = _require_column(
            filtered,
            op.value_column or op.column,
            plan,
        )

        years = [
            op.from_year,
            op.to_year,
        ]

        if (
            years[0] is None
            or years[1] is None
        ):
            if len(plan.years) >= 2:
                years = [
                    min(plan.years),
                    max(plan.years),
                ]
            else:
                raise AnalysisError(
                    "Year-over-year comparison requires two years."
                )

        old_year = int(years[0])
        new_year = int(years[1])

        old_v = _year_value(
            filtered,
            year_col,
            value_col,
            old_year,
        )

        new_v = _year_value(
            filtered,
            year_col,
            value_col,
            new_year,
        )

        if old_v == 0:
            raise AnalysisError(
                "Cannot compute percentage change from a zero baseline."
            )

        pct = (
            (new_v - old_v)
            / old_v
        ) * 100

        formula = (
            f"(({new_v} - {old_v}) "
            f"/ {old_v}) * 100"
        )

        return filtered, AnalysisResult(
            operation=op.op,
            value=_clean_number(pct),
            formula=formula,
            source_file=source_file,
            rows_used=2,
            columns_used=[
                year_col,
                value_col,
            ],
            inputs={
                "from": old_year,
                "to": new_year,
                "old": old_v,
                "new": new_v,
            },
        )

    # ---------------------------------------------------------
    # COMPARE
    # ---------------------------------------------------------

    if op.op == "compare":
        filtered = _apply_plan_filters(
            frame,
            plan,
        )

        column = _require_column(
            filtered,
            op.value_column or op.column,
            plan,
        )

        entity_col = (
            op.target
            or _guess_entity_column(filtered)
        )

        if (
            not entity_col
            or len(plan.entities) < 2
        ):
            raise AnalysisError(
                "Entity comparison requires two entities."
            )

        values = {}

        for entity in plan.entities[:2]:
            subset = _filter_frame(
                filtered,
                entity_col,
                entity,
            )

            series = pd.to_numeric(
                subset[column],
                errors="coerce",
            ).dropna()

            if series.empty:
                raise AnalysisError(
                    f"No numeric values found for {entity}."
                )

            values[entity] = _clean_number(
                float(
                    series.sum()
                    if len(series) > 1
                    else series.iloc[0]
                )
            )

        return filtered, AnalysisResult(
            operation="compare",
            value=values,
            source_file=source_file,
            rows_used=len(filtered),
            columns_used=[
                entity_col,
                column,
            ],
            inputs={
                "entities": plan.entities[:2],
            },
        )

    raise AnalysisError(
        f"Unsupported analysis operation '{op.op}'."
    )


# =============================================================
# PLAN FILTERS
# =============================================================


def _apply_plan_filters(
    frame: pd.DataFrame,
    plan: QueryPlan,
) -> pd.DataFrame:
    working = frame

    # ---------------------------------------------------------
    # Apply explicit filters generated by the planner.
    # ---------------------------------------------------------

    for column_name, value in plan.filters.items():
        column = _resolve_column(
            working,
            str(column_name),
        )

        if column is None:
            continue

        working = _filter_frame(
            working,
            column,
            value,
        )

    # ---------------------------------------------------------
    # Entity filter.
    #
    # This is useful when Gemini identifies:
    #
    #   entities = ["Carretera"]
    #
    # but does not explicitly create:
    #
    #   filters = {"Product": "Carretera"}
    # ---------------------------------------------------------

    entity_col = _guess_entity_column(
        working
    )

    if (
        entity_col
        and len(plan.entities) == 1
        and not _has_explicit_filter_for_column(
            plan,
            entity_col,
        )
    ):
        working = _filter_frame(
            working,
            entity_col,
            plan.entities[0],
        )

    # ---------------------------------------------------------
    # Year filter.
    # ---------------------------------------------------------

    if len(plan.years) == 1:
        year_col = _find_year_column(
            working
        )

        if year_col:
            years = pd.to_numeric(
                working[year_col],
                errors="coerce",
            )

            working = working[
                years == plan.years[0]
            ]

    return working


def _has_explicit_filter_for_column(
    plan: QueryPlan,
    column: str,
) -> bool:
    for key in plan.filters:
        if key.lower() == column.lower():
            return True

    return False


# =============================================================
# FILTER RESOLUTION
# =============================================================


def _resolve_filter_column(
    frame: pd.DataFrame,
    op: PlanOp,
    plan: QueryPlan,
) -> str:
    """
    Resolve the actual column to filter.

    If Gemini accidentally produces:

        column = "Units Sold"
        value = "Carretera"

    we recognize that "Units Sold" is probably the requested
    metric and "Carretera" is an entity value.

    We then use the dataset's entity column instead.
    """

    requested = op.column

    if requested:
        resolved = _resolve_column(
            frame,
            requested,
        )

        if resolved:
            # If the requested column is numeric but the value
            # is textual and matches an entity, it is probably
            # an incorrectly constructed filter.
            if (
                op.value is not None
                and _looks_numeric_column(frame, resolved)
                and _looks_like_entity_value(
                    frame,
                    op.value,
                    plan,
                )
            ):
                entity_col = _guess_entity_column(
                    frame
                )

                if entity_col:
                    return entity_col

            return resolved

    entity_col = _guess_entity_column(
        frame
    )

    if (
        entity_col
        and op.value is not None
        and _looks_like_entity_value(
            frame,
            op.value,
            plan,
        )
    ):
        return entity_col

    if plan.entities:
        if entity_col:
            return entity_col

    raise AnalysisError(
        "A filter column could not be determined."
    )


def _resolve_filter_value(
    frame: pd.DataFrame,
    column: str,
    op: PlanOp,
    plan: QueryPlan,
) -> Any:
    if op.value is not None:
        value = op.value

        # If the planner accidentally put the entity into
        # the metric filter, use the entity as the value.
        if (
            _looks_numeric_column(
                frame,
                column,
            )
            and _looks_like_entity_value(
                frame,
                value,
                plan,
            )
        ):
            if plan.entities:
                return plan.entities[0]

        return value

    if plan.entities:
        return plan.entities[0]

    raise AnalysisError(
        "A filter value was not provided."
    )


# =============================================================
# ENTITY / COLUMN HELPERS
# =============================================================


def _guess_entity_column(
    frame: pd.DataFrame,
) -> str | None:
    preferred = {
        "product",
        "product name",
        "department",
        "entity",
        "name",
        "team",
        "division",
        "company",
    }

    for column in frame.columns:
        if column.lower().strip() in preferred:
            return column

    for column in frame.columns:
        name = column.lower()

        if (
            "product" in name
            or "department" in name
            or "entity" in name
        ):
            return column

    return None


def _looks_numeric_column(
    frame: pd.DataFrame,
    column: str,
) -> bool:
    numeric = pd.to_numeric(
        frame[column],
        errors="coerce",
    )

    return numeric.notna().sum() > 0


def _looks_like_entity_value(
    frame: pd.DataFrame,
    value: Any,
    plan: QueryPlan,
) -> bool:
    if not isinstance(value, str):
        return False

    value = value.strip()

    if not value:
        return False

    if value in plan.entities:
        return True

    entity_col = _guess_entity_column(
        frame
    )

    if not entity_col:
        return False

    values = (
        frame[entity_col]
        .astype(str)
        .str.strip()
        .str.lower()
    )

    return value.lower() in set(values)


# =============================================================
# GENERAL COLUMN HELPERS
# =============================================================


def _filter_frame(
    frame: pd.DataFrame,
    column: str,
    value: Any,
) -> pd.DataFrame:
    series = (
        frame[column]
        .astype(str)
        .str.strip()
    )

    target = str(value).strip()

    # Normal textual/date comparison.
    exact = series.str.lower() == target.lower()

    if exact.any():
        return frame[exact]

    # Date normalization.
    try:
        target_date = pd.to_datetime(
            target,
            errors="raise",
        ).normalize()

        dates = pd.to_datetime(
            frame[column],
            errors="coerce",
        ).dt.normalize()

        date_matches = dates == target_date

        if date_matches.any():
            return frame[date_matches]

    except Exception:
        pass

    return frame[exact]


def _require_column(
    frame: pd.DataFrame,
    name: str | None,
    plan: QueryPlan,
) -> str:
    if not name:
        name = (
            plan.metrics[0]
            if plan.metrics
            else None
        )

    if not name:
        numeric = [
            column
            for column in frame.columns
            if pd.api.types.is_numeric_dtype(
                frame[column]
            )
        ]

        if len(numeric) == 1:
            return numeric[0]

        raise AnalysisError(
            "A target column was not specified."
        )

    resolved = _resolve_column(
        frame,
        name,
    )

    if resolved is None:
        raise AnalysisError(
            f"Column '{name}' was not found in the dataset."
        )

    return resolved


def _resolve_column(
    frame: pd.DataFrame,
    name: str,
) -> str | None:
    lookup = {
        column.lower(): column
        for column in frame.columns
    }

    name_lower = name.lower().strip()

    if name_lower in lookup:
        return lookup[name_lower]

    for column in frame.columns:
        column_lower = column.lower()

        if (
            name_lower in column_lower
            or column_lower in name_lower
        ):
            return column

    return None


def _find_year_column(
    frame: pd.DataFrame,
) -> str | None:
    for column in frame.columns:
        if column.lower().strip() in {
            "year",
            "yr",
            "fiscal_year",
            "fy",
        }:
            return column

    return None


def _year_column(
    frame: pd.DataFrame,
    op: PlanOp,
) -> str:
    if op.year_column:
        resolved = _resolve_column(
            frame,
            op.year_column,
        )

        if resolved:
            return resolved

    column = _find_year_column(frame)

    if column:
        return column

    raise AnalysisError(
        "A year column was not found in the dataset."
    )


def _year_value(
    frame: pd.DataFrame,
    year_col: str,
    value_col: str,
    year: int,
) -> float:
    years = pd.to_numeric(
        frame[year_col],
        errors="coerce",
    )

    subset = frame[
        years == year
    ]

    if subset.empty:
        raise AnalysisError(
            f"No rows were found for year {year}."
        )

    series = pd.to_numeric(
        subset[value_col],
        errors="coerce",
    ).dropna()

    if series.empty:
        raise AnalysisError(
            f"No numeric values were found for year {year}."
        )

    return float(
        series.sum()
        if len(series) > 1
        else series.iloc[0]
    )


def _clean_number(
    value: float | int,
) -> float | int:
    if (
        isinstance(value, float)
        and value.is_integer()
    ):
        return int(value)

    if isinstance(value, float):
        return round(value, 6)

    return value
