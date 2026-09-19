from __future__ import annotations

import logging
import re
from collections import defaultdict

from app.models import AnalysisResult, ConflictReport, Evidence, NumericClaim, Source

log = logging.getLogger(__name__)

MISSING_ANSWER = "The requested information was not found in the available documents."
OUT_OF_SCOPE_ANSWER = "The requested information is outside the available document evidence."

M_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def sources_from_evidence(evidence: list[Evidence], analysis: list[AnalysisResult] | None = None) -> list[Source]:
    sources: list[Source] = []
    seen: set[str] = set()
    for item in evidence:
        key = f"{item.document_id}:{item.chunk_id}"
        if key in seen:
            continue
        seen.add(key)
        excerpt = item.text.strip()
        if len(excerpt) > 400:
            excerpt = excerpt[:397] + "..."
        sources.append(
            Source(
                filename=item.filename,
                document_type=item.document_type,
                source_reference=item.source_reference,
                excerpt=excerpt,
                page_number=item.page_number or None,
                section=item.section or None,
                columns=item.columns or None,
                rows=f"{item.row_start}-{item.row_end}" if item.row_start else None,
            )
        )
    if analysis:
        for result in analysis:
            if not result.source_file:
                continue
            key = f"csv:{result.source_file}:{result.operation}"
            if key in seen:
                continue
            seen.add(key)
            sources.append(
                Source(
                    filename=result.source_file,
                    document_type="excel" if result.source_file.lower().endswith((".xlsx", ".xls")) else "csv",
                    source_reference=f"{result.source_file} ({result.operation})",
                    excerpt=result.formula or str(result.value),
                    columns=", ".join(result.columns_used) or None,
                    rows=str(result.rows_used) if result.rows_used else None,
                )
            )
    return sources


def validate_evidence(evidence: list[Evidence], analysis: list[AnalysisResult] | None) -> bool:
    if analysis and any(r.operation not in {"load_csv", "filter"} for r in analysis):
        return True
    return bool(evidence)


def detect_conflicts(evidence: list[Evidence], analysis: list[AnalysisResult] | None = None) -> ConflictReport:
    claims: list[NumericClaim] = []
    for item in evidence:
        claims.extend(_claims_from_text(item))
    if analysis:
        for result in analysis:
            if isinstance(result.value, (int, float)) and result.operation not in {"load_csv", "filter"}:
                year = None
                if isinstance(result.inputs.get("to"), int):
                    year = result.inputs.get("to")
                claims.append(
                    NumericClaim(
                        metric=result.operation,
                        year=year,
                        value=float(result.value),
                        source_filename=result.source_file or "csv",
                        source_reference=result.source_file or "csv",
                        excerpt=result.formula or str(result.value),
                    )
                )

    groups: dict[tuple[str, str | None, int | None, str | None], list[NumericClaim]] = defaultdict(list)
    for claim in claims:
        key = (
            claim.metric.lower(),
            (claim.entity or "").lower() or None,
            claim.year,
            (claim.unit or "").lower() or None,
        )
        groups[key].append(claim)

    genuine: list[NumericClaim] = []
    explanations: list[str] = []
    for key, group in groups.items():
        values = {round(c.value, 4) for c in group}
        files = {c.source_filename for c in group}
        if len(values) > 1 and len(files) > 1:
            genuine.extend(group)
            metric, entity, year, unit = key
            label = metric
            if entity:
                label = f"{entity} {label}"
            period = f" for {year}" if year else ""
            unit_s = f" {unit}" if unit else ""
            parts = [f"{c.source_filename} reports {c.value}{unit_s}" for c in group]
            explanations.append(
                f"{label}{period}: " + ", while ".join(parts) + ". The documents contain conflicting values."
            )

    report = ConflictReport(
        genuine=bool(genuine),
        explanation=" ".join(explanations),
        claims=genuine or claims,
    )
    if report.genuine:
        log.info("conflict_detection genuine=true claims=%s", len(genuine))
    else:
        log.info("conflict_detection genuine=false")
    return report


def _claims_from_text(item: Evidence) -> list[NumericClaim]:
    claims: list[NumericClaim] = []
    text = item.text
    year = item.year or _year(text)
    entity = item.entities or _entity(text)
    metric_match = re.search(
        r"(?i)\b(revenue|profit|income|sales|salary|total|amount|cost|expense)\b",
        text,
    )
    metric = metric_match.group(1).lower() if metric_match else ""
    if not metric:
        return claims
    for match in re.finditer(
        r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*([kmb])?|\b(\d{1,3}(?:,\d{3})+)(?:\.\d+)?\s*([kmb])?\b",
        text,
        re.I,
    ):
        raw = match.group(1) or match.group(3)
        suffix = (match.group(2) or match.group(4) or "").lower()
        if not raw:
            continue
        number = float(raw.replace(",", ""))
        if suffix in M_SUFFIX:
            number *= M_SUFFIX[suffix]
        has_dollar = match.group(0).strip().startswith("$")
        claims.append(
            NumericClaim(
                metric=metric,
                entity=entity or None,
                year=year or None,
                value=number,
                unit=("USD" if has_dollar else None),
                source_filename=item.filename,
                source_reference=item.source_reference,
                excerpt=item.text[:240],
            )
        )
    return claims


def _year(text: str) -> int:
    match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
    return int(match.group(1)) if match else 0


def _entity(text: str) -> str:
    near = re.search(
        r"\b(Engineering|Sales|Marketing|Finance|HR|Operations)\b(?=\s+revenue)",
        text,
        re.I,
    )
    if near:
        return near.group(1)
    match = re.search(r"\b(Engineering|Sales|Marketing|HR|Operations)\b", text, re.I)
    return match.group(1) if match else ""


