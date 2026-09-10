"""Whitelisted pandas operations for reliable local CSV analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.exceptions import PlanningError
from app.core.models import CsvAnalysisPlan, CsvAnalysisResult, DocumentRecord, DocumentType, SourceReference
from app.ingestion.csv_ingestor import read_csv_safely


class CsvAnalysisEngine:
    """Execute validated analysis plans without evaluating free-form code or SQL."""

    def analyze(self, record: DocumentRecord, plan: CsvAnalysisPlan) -> CsvAnalysisResult:
        if record.document_type is not DocumentType.CSV:
            raise PlanningError(f"'{record.filename}' is not a CSV dataset.")
        dataframe = read_csv_safely(Path(record.path))
        working = dataframe.copy()
        details: list[str] = [f"Loaded {len(working)} rows from {record.filename}."]
        used_columns: set[str] = set()

        for condition in plan.filters:
            self._assert_column(working, condition.column)
            used_columns.add(condition.column)
            working = self._apply_filter(working, condition.column, condition.operator, condition.value)
            details.append(f"Filtered {condition.column} {condition.operator} {condition.value!r}: {len(working)} rows remain.")

        if working.empty:
            details.append("No rows matched the requested filters.")
        source_rows = [int(index) + 2 for index in working.index[:20]]
        result = self._aggregate(working, plan, used_columns)

        for calculation in plan.derived_calculations:
            result = self._apply_derived_calculation(result, calculation.model_dump(), used_columns)
            details.append(f"Calculated {calculation.alias} using {calculation.operation}.")

        if plan.sort_by:
            self._assert_column(result, plan.sort_by)
            result = result.sort_values(plan.sort_by, ascending=not plan.sort_descending, kind="stable")
            used_columns.add(plan.sort_by)
            details.append(f"Sorted by {plan.sort_by} ({'descending' if plan.sort_descending else 'ascending'}).")
        if plan.limit:
            result = result.head(plan.limit)
            details.append(f"Limited output to {plan.limit} rows.")

        serializable_rows = json.loads(result.to_json(orient="records", date_format="iso"))
        calculation_details = " ".join(details)
        source = SourceReference(
            source_id=f"csv-{record.document_id}",
            filename=record.filename,
            document_type=DocumentType.CSV,
            columns=sorted(used_columns),
            rows=source_rows,
            calculation_details=calculation_details,
        )
        return CsvAnalysisResult(
            document_id=record.document_id,
            filename=record.filename,
            columns_used=sorted(used_columns),
            row_count=len(working),
            result_rows=serializable_rows,
            calculation_details=calculation_details,
            source=source,
        )

    def _aggregate(self, dataframe: pd.DataFrame, plan: CsvAnalysisPlan, used_columns: set[str]) -> pd.DataFrame:
        for column in plan.group_by:
            self._assert_column(dataframe, column)
            used_columns.add(column)
        for spec in plan.aggregations:
            self._assert_column(dataframe, spec.column)
            used_columns.add(spec.column)

        if not plan.aggregations:
            if plan.group_by:
                return dataframe[plan.group_by].drop_duplicates().reset_index(drop=True)
            return dataframe.reset_index(drop=True)

        named_aggregations = {
            spec.alias or f"{spec.operation}_{spec.column}": (spec.column, spec.operation)
            for spec in plan.aggregations
        }
        if plan.group_by:
            return dataframe.groupby(plan.group_by, dropna=False).agg(**named_aggregations).reset_index()
        # DataFrame.agg with named aggregations returns a layout that varies by
        # pandas version. Calculate each scalar explicitly for one stable row.
        totals = {
            spec.alias or f"{spec.operation}_{spec.column}": dataframe[spec.column].agg(spec.operation)
            for spec in plan.aggregations
        }
        return pd.DataFrame([totals])

    def _apply_filter(
        self, dataframe: pd.DataFrame, column: str, operator: str, value: Any
    ) -> pd.DataFrame:
        series = dataframe[column]
        if operator == "eq":
            return dataframe[series == self._coerce_value(series, value)]
        if operator == "ne":
            return dataframe[series != self._coerce_value(series, value)]
        if operator == "gt":
            return dataframe[series > self._coerce_value(series, value)]
        if operator == "gte":
            return dataframe[series >= self._coerce_value(series, value)]
        if operator == "lt":
            return dataframe[series < self._coerce_value(series, value)]
        if operator == "lte":
            return dataframe[series <= self._coerce_value(series, value)]
        if operator == "contains":
            return dataframe[series.astype(str).str.contains(str(value), case=False, na=False, regex=False)]
        if operator == "in":
            if not isinstance(value, list):
                raise PlanningError("The 'in' filter requires a list of values.")
            values = [self._coerce_value(series, item) for item in value]
            return dataframe[series.isin(values)]
        raise PlanningError(f"Unsupported filter operator: {operator}")

    @staticmethod
    def _coerce_value(series: pd.Series, value: Any) -> Any:
        if pd.api.types.is_numeric_dtype(series):
            try:
                return pd.to_numeric(value)
            except (TypeError, ValueError) as exc:
                raise PlanningError(f"Cannot compare numeric column '{series.name}' with {value!r}.") from exc
        if pd.api.types.is_bool_dtype(series):
            return str(value).lower() in {"true", "1", "yes"}
        return value

    def _apply_derived_calculation(
        self, dataframe: pd.DataFrame, calculation: dict[str, Any], used_columns: set[str]
    ) -> pd.DataFrame:
        operation = calculation["operation"]
        alias = calculation["alias"]
        if operation == "percentage_change_previous":
            target = calculation.get("target_column")
            if not target:
                raise PlanningError("percentage_change_previous requires target_column.")
            self._assert_column(dataframe, target)
            dataframe[alias] = dataframe[target].pct_change() * 100
            used_columns.add(target)
            used_columns.add(alias)
            return dataframe
        left = calculation.get("left_column")
        right = calculation.get("right_column")
        if not left or not right:
            raise PlanningError(f"{operation} requires left_column and right_column.")
        self._assert_column(dataframe, left)
        self._assert_column(dataframe, right)
        if operation == "difference":
            dataframe[alias] = dataframe[left] - dataframe[right]
        elif operation == "ratio":
            dataframe[alias] = dataframe[left] / dataframe[right].replace(0, float("nan"))
        elif operation == "percentage_change":
            dataframe[alias] = ((dataframe[left] - dataframe[right]) / dataframe[right].replace(0, float("nan"))) * 100
        else:
            raise PlanningError(f"Unsupported calculation operation: {operation}")
        used_columns.update({left, right, alias})
        return dataframe

    @staticmethod
    def _assert_column(dataframe: pd.DataFrame, column: str) -> None:
        if column not in dataframe.columns:
            available = ", ".join(str(item) for item in dataframe.columns)
            raise PlanningError(f"Column '{column}' does not exist. Available columns: {available}.")
