from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from app.analyze import needs_analysis, run_analysis
from app.evidence import (
    MISSING_ANSWER,
    OUT_OF_SCOPE_ANSWER,
    detect_conflicts,
    sources_from_evidence,
    validate_evidence,
)
from app.errors import AnalysisError, GeminiError
from app.gemini import GeminiService
from app.models import (
    AnalysisResult,
    ChatResponse,
    ConversationContext,
    Evidence,
    ExecutionMode,
    QueryPlan,
    Source,
)
from app.plan import build_plan
from app.retrieve import retrieve_evidence
from app.storage import Storage, utcnow

log = logging.getLogger(__name__)


ANSWER_SYSTEM = """
You are the final answer component of a document-analysis system.

Answer the user's question using ONLY the evidence supplied to you.

Evidence may contain:

1. RETRIEVED DOCUMENT EVIDENCE
   Text retrieved from uploaded PDF/TXT documents.

2. VERIFIED STRUCTURED ANALYSIS
   Results calculated by DuckDB against uploaded CSV/Excel datasets.

3. CONFLICT INFORMATION
   Information about conflicting claims found across uploaded sources.

CRITICAL RULES

1. Do not use outside knowledge to answer the user's document question.

2. Do not invent facts, values, rows, calculations, document claims,
   explanations, or citations.

3. Treat VERIFIED STRUCTURED ANALYSIS as computationally authoritative for
   calculations performed against the uploaded structured datasets.

4. Do not recalculate structured numerical results yourself when a verified
   result is already supplied.

5. You may explain, summarize, compare, and interpret verified structured
   results, but preserve the actual computed values.

6. Retrieved text is evidence from uploaded documents. Attribute statements
   to that evidence when appropriate.

7. For hybrid questions, combine retrieved textual evidence with verified
   structured results only when the supplied evidence supports the connection.

8. If the supplied evidence does not contain enough information to answer,
   return status "missing" and use exactly this answer:

The requested information was not found in the available documents.

9. If the request is outside the available document evidence, return status
   "out_of_scope" and use exactly this answer:

The requested information is outside the available document evidence.

10. If the question is ambiguous and cannot be answered reliably, return
    status "ambiguous". Briefly explain what is ambiguous.

11. If sources materially disagree, describe the disagreement clearly rather
    than silently choosing one value.

12. If a structured query returned zero rows, do not invent a value. Explain
    that no matching rows were found if that directly answers the question.

13. If a structured result was truncated, do not claim that the displayed
    subset is the entire result.

14. Be concise but complete.

Return JSON only:

{
  "status": "answered | missing | out_of_scope | ambiguous | conflict",
  "answer": "final answer"
}
"""


def ask(
    question: str,
    conversation_id: str | None,
    store: Storage,
    gemini: GeminiService,
) -> ChatResponse:
    """
    Main document-question orchestration.

    Pipeline:

        question
            ↓
        high-level planner
            ↓
        ┌──────────────┬────────────────┬───────────────┐
        │ retrieval    │ structured     │ hybrid        │
        │ PDF/TXT      │ CSV/Excel      │ both          │
        └──────────────┴────────────────┴───────────────┘
            ↓
        grounded evidence
            ↓
        final answer
            ↓
        conversation state
    """

    question = question.strip()

    conversation_id, context = _conversation(
        conversation_id=conversation_id,
        store=store,
    )

    user_message_id = str(uuid.uuid4())

    store.add_message(
        {
            "id": user_message_id,
            "conversation_id": conversation_id,
            "role": "user",
            "content": question,
            "created_at": utcnow(),
        }
    )

    execution_flow: list[str] = [
        "question_received",
        "planning",
    ]

    try:
        plan = build_plan(
            question=question,
            store=store,
            gemini=gemini,
            context=context,
        )
    except Exception:
        log.exception(
            "planning_failed conversation_id=%s",
            conversation_id,
        )

        return _finalize(
            conversation_id=conversation_id,
            question=question,
            answer=MISSING_ANSWER,
            status="missing",
            sources=[],
            execution_flow=[
                *execution_flow,
                "planning_failed",
            ],
            plan=None,
            context=context,
            store=store,
            analysis=[],
        )

    execution_flow.append(
        f"mode:{plan.mode.value}"
    )

    if plan.out_of_scope:
        return _finalize(
            conversation_id=conversation_id,
            question=question,
            answer=OUT_OF_SCOPE_ANSWER,
            status="out_of_scope",
            sources=[],
            execution_flow=[
                *execution_flow,
                "out_of_scope",
            ],
            plan=plan,
            context=context,
            store=store,
            analysis=[],
        )

    if plan.ambiguous:
        answer = (
            plan.ambiguity_reason
            or "The question is ambiguous for the available documents."
        )

        return _finalize(
            conversation_id=conversation_id,
            question=question,
            answer=answer,
            status="ambiguous",
            sources=[],
            execution_flow=[
                *execution_flow,
                "ambiguous",
            ],
            plan=plan,
            context=context,
            store=store,
            analysis=[],
        )

    evidence: list[Evidence] = []
    analysis: list[AnalysisResult] = []

    # ------------------------------------------------------------------
    # Textual evidence path
    # ------------------------------------------------------------------

    if plan.mode in {
        ExecutionMode.RETRIEVAL,
        ExecutionMode.HYBRID,
    }:
        execution_flow.append(
            "retrieving_document_evidence"
        )

        try:
            evidence = retrieve_evidence(
                plan=plan,
                question=question,
                store=store,
                gemini=gemini,
                settings=store.settings,
            )
        except Exception:
            log.exception(
                "retrieval_failed conversation_id=%s",
                conversation_id,
            )

            evidence = []

        execution_flow.append(
            f"retrieved:{len(evidence)}"
        )

    # ------------------------------------------------------------------
    # Structured-data path
    # ------------------------------------------------------------------

    if needs_analysis(plan):
        execution_flow.append(
            "analyzing_structured_data"
        )

        try:
            analysis = run_analysis(
                question=question,
                plan=plan,
                store=store,
                gemini=gemini,
                max_repair_attempts=getattr(
                    store.settings,
                    "analysis_max_repair_attempts",
                    2,
                ),
                max_result_rows=getattr(
                    store.settings,
                    "analysis_max_result_rows",
                    200,
                ),
            )

            execution_flow.append(
                "structured_analysis_verified"
            )

        except AnalysisError as exc:
            log.warning(
                "structured_analysis_failed "
                "conversation_id=%s error=%s",
                conversation_id,
                str(exc),
            )

            execution_flow.append(
                "structured_analysis_failed"
            )

            analysis = []

        except Exception:
            log.exception(
                "structured_analysis_unexpected_failure "
                "conversation_id=%s",
                conversation_id,
            )

            execution_flow.append(
                "structured_analysis_failed"
            )

            analysis = []

    # ------------------------------------------------------------------
    # Evidence validation
    # ------------------------------------------------------------------

    if not validate_evidence(
        plan=plan,
        evidence=evidence,
        analysis=analysis,
    ):
        return _finalize(
            conversation_id=conversation_id,
            question=question,
            answer=MISSING_ANSWER,
            status="missing",
            sources=[],
            execution_flow=[
                *execution_flow,
                "insufficient_evidence",
            ],
            plan=plan,
            context=context,
            store=store,
            analysis=analysis,
        )

    execution_flow.append(
        "evidence_validated"
    )

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    conflict_report = detect_conflicts(
        evidence=evidence,
        analysis=analysis,
    )

    if conflict_report.has_conflicts:
        execution_flow.append(
            "conflicts_detected"
        )

    # ------------------------------------------------------------------
    # Final grounded answer
    # ------------------------------------------------------------------

    answer_payload = _answer_with_gemini(
        question=question,
        plan=plan,
        evidence=evidence,
        analysis=analysis,
        conflict_report=conflict_report.model_dump(
            mode="json"
        ),
        gemini=gemini,
    )

    status = _normalize_status(
        answer_payload.get("status")
    )

    answer = str(
        answer_payload.get("answer") or ""
    ).strip()

    if not answer:
        answer, status = _fallback_answer(
            plan=plan,
            evidence=evidence,
            analysis=analysis,
        )

    if (
        conflict_report.has_conflicts
        and status == "answered"
    ):
        status = "conflict"

    sources = sources_from_evidence(
        evidence=evidence,
        analysis=analysis,
    )

    execution_flow.append(
        "answer_generated"
    )

    return _finalize(
        conversation_id=conversation_id,
        question=question,
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        plan=plan,
        context=context,
        store=store,
        analysis=analysis,
    )


def _answer_with_gemini(
    *,
    question: str,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
    conflict_report: dict[str, Any],
    gemini: GeminiService,
) -> dict[str, Any]:
    prompt = _answer_prompt(
        question=question,
        plan=plan,
        evidence=evidence,
        analysis=analysis,
        conflict_report=conflict_report,
    )

    try:
        payload = gemini.generate_json(
            system=ANSWER_SYSTEM,
            user=prompt,
        )

        if isinstance(payload, dict):
            return payload

    except GeminiError:
        log.exception(
            "final_answer_generation_failed"
        )

    except Exception:
        log.exception(
            "final_answer_payload_invalid"
        )

    answer, status = _fallback_answer(
        plan=plan,
        evidence=evidence,
        analysis=analysis,
    )

    return {
        "status": status,
        "answer": answer,
    }


def _answer_prompt(
    *,
    question: str,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
    conflict_report: dict[str, Any],
) -> str:
    retrieved_payload = [
        {
            "document_id": item.document_id,
            "filename": item.filename,
            "document_type": item.document_type,
            "source_reference": item.source_reference,
            "page_number": item.page_number,
            "section": item.section,
            "start_line": item.start_line,
            "end_line": item.end_line,
            "text": item.text,
            "similarity": item.similarity,
        }
        for item in evidence
    ]

    structured_payload = [
        {
            "operation": item.operation,
            "value": item.value,
            "columns": item.columns,
            "rows": item.table,
            "query": item.query,
            "source_filenames": (
                item.source_filenames
                or (
                    [item.filename]
                    if item.filename
                    else []
                )
            ),
            "rows_used": item.rows_used,
            "truncated": item.truncated,
            "explanation": item.explanation,
        }
        for item in analysis
    ]

    parts = [
        "USER QUESTION:",
        question,
        "",
        "QUERY PLAN:",
        json.dumps(
            plan.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "",
        "RETRIEVED DOCUMENT EVIDENCE:",
        json.dumps(
            retrieved_payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "",
        "VERIFIED STRUCTURED ANALYSIS:",
        json.dumps(
            structured_payload,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "",
        "CONFLICT REPORT:",
        json.dumps(
            conflict_report,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        "",
        (
            "Answer using only the evidence above. "
            "Do not perform new unsupported calculations."
        ),
    ]

    return "\n".join(parts)


def _fallback_answer(
    *,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
) -> tuple[str, str]:
    """
    Deterministic fallback when final Gemini generation fails.

    For structured scalar/table results we can still return the verified
    computation instead of losing a successfully executed analysis.
    """

    if analysis:
        result = analysis[0]

        if result.value is not None:
            return (
                _format_scalar(result.value),
                "answered",
            )

        if result.table:
            return (
                _format_table_result(result),
                "answered",
            )

        # A successful structured query with zero rows is still meaningful.
        if result.rows_used == 0:
            return (
                "No matching rows were found in the available structured data.",
                "answered",
            )

    if evidence:
        # We deliberately do not try to invent a textual synthesis here.
        # Gemini failed, so returning raw evidence as though it were an answer
        # could misrepresent the document.
        return MISSING_ANSWER, "missing"

    return MISSING_ANSWER, "missing"


def _format_scalar(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"

    return str(value)


def _format_table_result(
    result: AnalysisResult,
) -> str:
    rows = result.table

    if not rows:
        return (
            "No matching rows were found in the available structured data."
        )

    columns = (
        result.columns
        or list(rows[0].keys())
    )

    lines: list[str] = []

    for row in rows:
        values = [
            f"{column}: {row.get(column)}"
            for column in columns
        ]

        lines.append(
            "; ".join(values)
        )

    if result.truncated:
        lines.append(
            "The displayed result was truncated."
        )

    return "\n".join(lines)


def _conversation(
    *,
    conversation_id: str | None,
    store: Storage,
) -> tuple[str, ConversationContext]:
    if conversation_id:
        try:
            record = store.get_conversation(
                conversation_id
            )

            context = ConversationContext.model_validate_json(
                record["context_json"]
            )

            return conversation_id, context

        except Exception:
            # A supplied but unknown conversation ID should not cause the
            # document question itself to fail. Start a new conversation.
            log.warning(
                "conversation_load_failed id=%s; creating new conversation",
                conversation_id,
            )

    new_id = str(uuid.uuid4())

    record = store.create_conversation(
        new_id
    )

    context = ConversationContext.model_validate_json(
        record["context_json"]
    )

    return new_id, context


def _finalize(
    *,
    conversation_id: str,
    question: str,
    answer: str,
    status: str,
    sources: list[Source],
    execution_flow: list[str],
    plan: QueryPlan | None,
    context: ConversationContext,
    store: Storage,
    analysis: list[AnalysisResult],
) -> ChatResponse:
    """
    Persist assistant response and update follow-up context.
    """

    updated_context = _updated_context(
        old=context,
        question=question,
        answer=answer,
        plan=plan,
        analysis=analysis,
    )

    store.update_conversation_context(
        conversation_id,
        updated_context,
    )

    assistant_message_id = str(
        uuid.uuid4()
    )

    store.add_message(
        {
            "id": assistant_message_id,
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": answer,
            "status": status,
            "sources_json": json.dumps(
                [
                    source.model_dump(
                        mode="json"
                    )
                    for source in sources
                ],
                ensure_ascii=False,
                default=str,
            ),
            "execution_flow_json": json.dumps(
                execution_flow,
                ensure_ascii=False,
            ),
            "query_plan_json": (
                plan.model_dump_json()
                if plan is not None
                else None
            ),
            "created_at": utcnow(),
        }
    )

    return ChatResponse(
        conversation_id=conversation_id,
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        query_plan=plan,
    )


def _updated_context(
    *,
    old: ConversationContext,
    question: str,
    answer: str,
    plan: QueryPlan | None,
    analysis: list[AnalysisResult],
) -> ConversationContext:
    if plan is None:
        return ConversationContext(
            entities=list(old.entities),
            metrics=list(old.metrics),
            years=list(old.years),
            document_ids=list(
                old.document_ids
            ),
            document_hints=list(
                old.document_hints
            ),
            last_question=question,
            last_answer=answer,
            last_execution_mode=(
                old.last_execution_mode
            ),
            numeric_results=dict(
                old.numeric_results
            ),
        )

    numeric_results = dict(
        old.numeric_results
    )

    for index, result in enumerate(
        analysis,
        start=1,
    ):
        if isinstance(
            result.value,
            (int, float),
        ) and not isinstance(
            result.value,
            bool,
        ):
            key = _numeric_result_key(
                result=result,
                index=index,
            )

            numeric_results[key] = float(
                result.value
            )

    return ConversationContext(
        entities=(
            list(plan.entities)
            if plan.entities
            else list(old.entities)
        ),
        metrics=(
            list(plan.metrics)
            if plan.metrics
            else list(old.metrics)
        ),
        years=(
            list(plan.years)
            if plan.years
            else list(old.years)
        ),
        document_ids=(
            list(plan.document_ids)
            if plan.document_ids
            else list(old.document_ids)
        ),
        document_hints=(
            list(plan.document_hints)
            if plan.document_hints
            else list(old.document_hints)
        ),
        last_question=question,
        last_answer=answer,
        last_execution_mode=plan.mode,
        numeric_results=numeric_results,
    )


def _numeric_result_key(
    *,
    result: AnalysisResult,
    index: int,
) -> str:
    if result.columns:
        if len(result.columns) == 1:
            return result.columns[0]

    return f"structured_result_{index}"


def _normalize_status(
    raw: Any,
) -> str:
    status = str(
        raw or "answered"
    ).strip().lower()

    allowed = {
        "answered",
        "missing",
        "out_of_scope",
        "ambiguous",
        "conflict",
    }

    if status not in allowed:
        return "answered"

    return status
