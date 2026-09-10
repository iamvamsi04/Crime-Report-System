"""Deterministic conflict detection for comparable numeric CSV results."""

from __future__ import annotations

from collections import defaultdict
from numbers import Number

from app.core.models import Conflict, CsvAnalysisResult


class ConflictDetector:
    """Report different numeric values for matching labeled records across datasets."""

    def from_csv_results(self, results: list[CsvAnalysisResult]) -> list[Conflict]:
        observations: dict[tuple[str, str], list[tuple[float, CsvAnalysisResult]]] = defaultdict(list)
        for result in results:
            for row in result.result_rows:
                for column, value in row.items():
                    if (
                        isinstance(value, Number)
                        and not isinstance(value, bool)
                        and not self._is_time_dimension(str(column))
                    ):
                        labels = tuple(
                            sorted(
                                (str(key), str(other_value))
                                for key, other_value in row.items()
                                if key != column
                                and (
                                    not isinstance(other_value, Number)
                                    or self._is_time_dimension(str(key))
                                )
                            )
                        )
                        observations[(str(labels), str(column))].append((float(value), result))

        conflicts: list[Conflict] = []
        for (label, column), values in observations.items():
            source_values = {(value, result.document_id) for value, result in values}
            if len({value for value, _ in source_values}) <= 1 or len({doc for _, doc in source_values}) <= 1:
                continue
            detail = "; ".join(f"{result.filename}: {value:g}" for value, result in values)
            conflicts.append(
                Conflict(
                    topic=f"{column} for {label}",
                    description=f"Conflicting values detected: {detail}.",
                    source_ids=[result.source.source_id for _, result in values],
                )
            )
        return conflicts

    @staticmethod
    def _is_time_dimension(column: str) -> bool:
        """Keep numeric years/periods in a comparison key, not as competing metrics."""
        lowered = column.lower()
        return any(token in lowered for token in ("year", "quarter", "month", "period", "date"))
