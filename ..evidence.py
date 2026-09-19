from __future__ import annotations

import math
import re
from typing import Any, Iterable

from app.models import AnalysisResult, Evidence, Source


_NUMERIC_RE = re.compile(
    r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?"
)


def sources_from_evidence(
    evidence: Iterable[Evidence],
    analysis: Iterable[AnalysisResult] | None = None,
) -> list[Source]:
    """
    Convert retrieved evidence and structured analysis results into
    source records exposed by the chat response.
    """
    sources: list[Source] = []
    seen: set[tuple[Any, ...]] = set()

    for item in evidence:
        source = _source_from_evidence(item)

        key = (
            source.document_id,
            source.filename,
            source.page,
            source.chunk_id,
        )

        if key not in seen:
            seen.add(key)
            sources.append(source)

    for result in analysis or []:
        source = Source(
            document_id=result.inputs.get("document_id")
            if isinstance(result.inputs, dict)
            else None,
            filename=result.source_file,
            page=None,
            chunk_id=None,
            excerpt=_analysis_excerpt(result),
        )

        key = (
            source.document_id,
            source.filename,
            source.page,
            source.chunk_id,
        )

        if key not in seen:
            seen.add(key)
            sources.append(source)

    return sources


def validate_evidence(
    evidence: Iterable[Evidence],
    analysis: Iterable[AnalysisResult],
) -> bool:
    """
    Determine whether the answer has sufficient grounded evidence.

    A successful structured analysis result is considered valid evidence
    for numerical/analytical questions. Retrieval evidence is considered
    valid when at least one usable item is present.
    """
    evidence_list = list(evidence)
    analysis_list = list(analysis)

    if evidence_list:
        return True

    for result in analysis_list:
        if _analysis_result_is_valid(result):
            return True

    return False


def detect_conflicts(
    evidence: Iterable[Evidence],
    analysis: Iterable[AnalysisResult],
) -> list[str]:
    """
    Detect obvious numeric conflicts between retrieved evidence and
    structured analysis results.

    This is intentionally conservative. It only reports conflicts when
    the same numeric value appears to be represented differently.
    """
    evidence_numbers = _extract_evidence_numbers(evidence)
    conflicts: list[str] = []

    for result in analysis:
        value_numbers = _extract_result_numbers(result)

        for value in value_numbers:
            if not _has_close_numeric_match(value, evidence_numbers):
                continue

        # The analysis result itself is authoritative for the structured
        # calculation. We only report explicit contradictory numeric
        # evidence when both sides contain comparable values.
        if evidence_numbers and value_numbers:
            unmatched = [
                value
                for value in value_numbers
                if not _has_close_numeric_match(value, evidence_numbers)
            ]

            if unmatched:
                conflicts.append(
                    (
                        f"Structured analysis from {result.source_file!r} "
                        f"contains numeric value(s) {unmatched}, while "
                        "retrieved evidence contains different numeric values."
                    )
                )

    return conflicts


def _source_from_evidence(item: Evidence) -> Source:
    """
    Build a Source object while tolerating the small differences that may
    exist between retrieval evidence records.
    """
    data = (
        item.model_dump()
        if hasattr(item, "model_dump")
        else dict(item)
    )

    return Source(
        document_id=data.get("document_id"),
        filename=data.get("filename"),
        page=data.get("page"),
        chunk_id=data.get("chunk_id"),
        excerpt=data.get("excerpt") or data.get("text"),
    )


def _analysis_result_is_valid(result: AnalysisResult) -> bool:
    """
    A load-only result is not useful as final evidence. A result containing
    an actual calculation, filtering, sorting, grouping, comparison, etc.
    is considered structured evidence.
    """
    operation = (result.operation or "").strip().lower()

    return operation not in {
        "",
        "load_csv",
        "load",
    }


def _analysis_excerpt(result: AnalysisResult) -> str:
    """
    Create a compact source excerpt from a structured analysis result.
    """
    parts: list[str] = []

    if result.operation:
        parts.append(f"Operation: {result.operation}")

    if result.value is not None:
        parts.append(f"Value: {result.value}")

    if result.table:
        parts.append(f"Rows: {len(result.table)}")

        # Keep source excerpts compact. The full structured result is
        # separately supplied to the answer-generation layer.
        preview = result.table[:5]

        for row in preview:
            parts.append(str(row))

    if result.formula:
        parts.append(f"SQL: {result.formula}")

    return "\n".join(parts)


def _extract_evidence_numbers(
    evidence: Iterable[Evidence],
) -> list[float]:
    numbers: list[float] = []

    for item in evidence:
        data = (
            item.model_dump()
            if hasattr(item, "model_dump")
            else dict(item)
        )

        for field in ("excerpt", "text", "content"):
            value = data.get(field)

            if not isinstance(value, str):
                continue

            numbers.extend(_extract_numbers(value))

    return numbers


def _extract_result_numbers(
    result: AnalysisResult,
) -> list[float]:
    numbers: list[float] = []

    if result.value is not None:
        numbers.extend(_extract_numbers(str(result.value)))

    if result.table:
        for row in result.table:
            numbers.extend(_extract_numbers(str(row)))

    return numbers


def _extract_numbers(text: str) -> list[float]:
    values: list[float] = []

    for match in _NUMERIC_RE.findall(text):
        try:
            values.append(float(match))
        except ValueError:
            continue

    return values


def _has_close_numeric_match(
    value: float,
    candidates: list[float],
) -> bool:
    for candidate in candidates:
        tolerance = max(
            1e-9,
            abs(value) * 1e-6,
        )

        if math.isclose(
            value,
            candidate,
            rel_tol=1e-6,
            abs_tol=tolerance,
        ):
            return True

    return False
