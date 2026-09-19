from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import AnalysisResult, Evidence, Source


MISSING_ANSWER = (
    "I could not find enough information in the uploaded documents "
    "to answer that question."
)

OUT_OF_SCOPE_ANSWER = (
    "I can answer questions based on the uploaded PDF, TXT, CSV, "
    "and Excel documents."
)


@dataclass(frozen=True)
class ConflictResult:
    genuine: bool
    explanation: str | None = None


def validate_evidence(
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> bool:
    """
    Determine whether there is enough grounded information to answer
    the user's question.

    Structured-data analysis is considered valid when the analysis layer
    produced at least one meaningful result.

    Document questions require retrieved evidence containing actual text.
    """

    if analysis:
        return any(_analysis_has_result(result) for result in analysis)

    if evidence:
        return any(_evidence_has_content(item) for item in evidence)

    return False


def sources_from_evidence(
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> list[Source]:
    """
    Convert internal evidence and analysis results into the public
    Source model returned by the API.
    """

    sources: list[Source] = []
    seen: set[tuple[str, str | None, str | None]] = set()

    for item in evidence:
        key = (
            item.filename,
            item.source_reference,
            item.chunk_id,
        )

        if key in seen:
            continue

        seen.add(key)

        sources.append(
            Source(
                filename=item.filename,
                document_type=item.document_type,
                source_reference=(
                    item.source_reference
                    or _default_source_reference(item)
                ),
                excerpt=_clean_excerpt(item.text),
                page_number=item.page_number,
                section=item.section,
                columns=list(item.columns),
                rows=[],
            )
        )

    for result in analysis:
        source = _source_from_analysis(result)

        if source is None:
            continue

        key = (
            source.filename,
            source.source_reference,
            None,
        )

        if key in seen:
            continue

        seen.add(key)
        sources.append(source)

    return sources


def detect_conflicts(
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> ConflictResult:
    """
    Detect clear conflicts in grounded information.

    This intentionally looks for explicit contradictions rather than
    treating different passages or different calculated values as
    automatically conflicting.
    """

    evidence_conflict = _detect_evidence_conflict(evidence)

    if evidence_conflict is not None:
        return ConflictResult(
            genuine=True,
            explanation=evidence_conflict,
        )

    analysis_conflict = _detect_analysis_conflict(analysis)

    if analysis_conflict is not None:
        return ConflictResult(
            genuine=True,
            explanation=analysis_conflict,
        )

    return ConflictResult(genuine=False)


def _analysis_has_result(result: AnalysisResult) -> bool:
    """
    Check whether an analysis result contains usable information.
    """

    if result.value is not None:
        return True

    if result.table:
        return True

    if result.inputs:
        return True

    return False


def _evidence_has_content(item: Evidence) -> bool:
    """
    Check whether retrieved document evidence contains usable text.
    """

    return bool(item.text and item.text.strip())


def _default_source_reference(item: Evidence) -> str:
    """
    Create a readable source reference when the retrieval layer did not
    already provide one.
    """

    if item.page_number is not None:
        return f"{item.filename}, page {item.page_number}"

    if item.section:
        return f"{item.filename}, {item.section}"

    if item.start_line is not None:
        if item.end_line is not None:
            return (
                f"{item.filename}, "
                f"lines {item.start_line}-{item.end_line}"
            )

        return f"{item.filename}, line {item.start_line}"

    if item.row_start is not None:
        if item.row_end is not None:
            return (
                f"{item.filename}, "
                f"rows {item.row_start}-{item.row_end}"
            )

        return f"{item.filename}, row {item.row_start}"

    return item.filename


def _clean_excerpt(text: str | None) -> str | None:
    """
    Keep source excerpts readable without changing their meaning.
    """

    if not text:
        return None

    cleaned = " ".join(text.split())

    if not cleaned:
        return None

    max_length = 1200

    if len(cleaned) <= max_length:
        return cleaned

    return cleaned[:max_length].rstrip() + "..."


def _source_from_analysis(
    result: AnalysisResult,
) -> Source | None:
    """
    Convert a structured-data analysis result into a source entry.

    AnalysisResult contains the source filename and the columns used,
    so the API can tell the user which dataset produced the calculation.
    """

    if not result.source_file:
        return None

    rows: list[str] = []

    if result.table:
        rows = [
            _format_table_row(row)
            for row in result.table[:20]
        ]

    source_reference = result.source_file

    if result.rows_used is not None:
        source_reference = (
            f"{result.source_file} "
            f"({result.rows_used} rows used)"
        )

    return Source(
        filename=result.source_file,
        document_type=_document_type_from_filename(result.source_file),
        source_reference=source_reference,
        excerpt=_analysis_excerpt(result),
        columns=list(result.columns_used),
        rows=rows,
    )


def _analysis_excerpt(
    result: AnalysisResult,
) -> str | None:
    """
    Produce a concise explanation of what the analysis result represents.
    """

    parts: list[str] = []

    if result.operation:
        parts.append(f"Operation: {result.operation}")

    if result.value is not None:
        parts.append(f"Result: {_format_value(result.value)}")

    if result.formula:
        parts.append(f"Formula: {result.formula}")

    if result.rows_used is not None:
        parts.append(f"Rows used: {result.rows_used}")

    if not parts:
        return None

    return "; ".join(parts)


def _format_table_row(row: dict[str, Any]) -> str:
    """
    Convert one analysis table row into a compact source string.
    """

    parts: list[str] = []

    for key, value in row.items():
        parts.append(f"{key}={_format_value(value)}")

    return ", ".join(parts)


def _format_value(value: Any) -> str:
    if value is None:
        return "null"

    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))

        return f"{value:.6f}".rstrip("0").rstrip(".")

    return str(value)


def _document_type_from_filename(filename: str) -> str:
    lower = filename.lower()

    if lower.endswith(".csv"):
        return "csv"

    if lower.endswith(".xlsx"):
        return "xlsx"

    if lower.endswith(".xls"):
        return "xls"

    if lower.endswith(".pdf"):
        return "pdf"

    if lower.endswith(".txt"):
        return "txt"

    return "unknown"


def _detect_evidence_conflict(
    evidence: list[Evidence],
) -> str | None:
    """
    Detect simple explicit numeric conflicts when the same source
    location appears with different extracted values.

    Retrieval can return multiple chunks from the same document. Merely
    having different text is not considered a conflict.
    """

    grouped: dict[str, list[Evidence]] = {}

    for item in evidence:
        key = (
            item.source_reference
            or item.chunk_id
            or f"{item.filename}:{item.page_number}:{item.section}"
        )

        grouped.setdefault(key, []).append(item)

    for reference, items in grouped.items():
        if len(items) < 2:
            continue

        normalized_texts = {
            " ".join(item.text.split()).strip().lower()
            for item in items
            if item.text and item.text.strip()
        }

        if len(normalized_texts) <= 1:
            continue

        if _texts_contain_direct_conflict(items):
            return (
                "The retrieved evidence contains different values or "
                f"statements for the same source location ({reference})."
            )

    return None


def _texts_contain_direct_conflict(
    items: list[Evidence],
) -> bool:
    """
    Look for a narrow class of direct numeric contradictions.

    This avoids declaring ordinary complementary document passages
    contradictory.
    """

    numeric_values: set[str] = set()

    for item in items:
        text = item.text or ""

        for token in _extract_numeric_tokens(text):
            numeric_values.add(token)

    return len(numeric_values) > 1


def _extract_numeric_tokens(text: str) -> set[str]:
    """
    Extract numeric tokens useful for detecting obvious contradictions.

    This is deliberately conservative and is not used to answer
    questions or perform calculations.
    """

    import re

    matches = re.findall(
        r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?%?",
        text,
    )

    return {
        value.replace(",", "")
        for value in matches
    }


def _detect_analysis_conflict(
    analysis: list[AnalysisResult],
) -> str | None:
    """
    Detect conflicting calculated results only when the same operation
    and source column are represented by multiple different scalar values.
    """

    scalar_results: dict[
        tuple[str, str | None, tuple[str, ...]],
        list[Any],
    ] = {}

    for result in analysis:
        if result.value is None:
            continue

        key = (
            result.operation,
            result.source_file,
            tuple(result.columns_used),
        )

        scalar_results.setdefault(key, []).append(result.value)

    for key, values in scalar_results.items():
        normalized = {
            _normalize_comparable_value(value)
            for value in values
        }

        if len(normalized) > 1:
            operation, source_file, columns = key

            source_text = source_file or "the available datasets"
            column_text = (
                ", ".join(columns)
                if columns
                else "the requested columns"
            )

            return (
                f"Different results were produced for the same "
                f"{operation} operation on {column_text} in "
                f"{source_text}."
            )

    return None


def _normalize_comparable_value(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 10)

    if isinstance(value, list):
        return tuple(
            _normalize_comparable_value(item)
            for item in value
        )

    if isinstance(value, dict):
        return tuple(
            sorted(
                (
                    key,
                    _normalize_comparable_value(item),
                )
                for key, item in value.items()
            )
        )

    return value
