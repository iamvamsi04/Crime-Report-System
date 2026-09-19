from __future__ import annotations

import json
import logging
from typing import Any

from app.analyze import needs_analysis, run_analysis
from app.errors import AppError, AnalysisError
from app.evidence import (
    MISSING_ANSWER,
    OUT_OF_SCOPE_ANSWER,
    detect_conflicts,
    sources_from_evidence,
    validate_evidence,
)
from app.gemini import GeminiService
from app.models import (
    AnalysisResult,
    ChatResponse,
    ChatStatus,
    ConversationContext,
    Evidence,
    MessageOut,
    QueryPlan,
    Source,
)
from app.plan import build_plan
from app.retrieve import retrieve_evidence
from app.storage import Storage
from app.config import Settings


log = logging.getLogger(__name__)


ANSWER_SYSTEM = """
You are the answer-generation component of an intelligent document analysis
system.

Your answer MUST be based only on the evidence and analysis results supplied
to you.

Rules:
1. Never invent facts, numbers, names, dates, or calculations.
2. Do not use outside knowledge.
3. For CSV/Excel questions, trust the supplied analysis result because it was
   calculated from the actual uploaded dataset.
4. For PDF/TXT questions, use only the supplied retrieved evidence.
5. If the supplied evidence does not contain enough information, say that the
   information is not available in the uploaded documents.
6. If analysis results contain a calculation, explain the result clearly.
7. When useful, mention the source document and relevant page/section.
8. Do not claim that you inspected a document if no evidence from that
   document was supplied.
9. Keep the answer concise but sufficiently explanatory.
"""


def ask(
    *,
    question: str,
    conversation_id: str | None,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> ChatResponse:
    """
    Process one user question.

    Flow:

        question
            ↓
        conversation/history
            ↓
        planner
            ↓
        structured analysis and/or RAG retrieval
            ↓
        evidence validation
            ↓
        Gemini answer
            ↓
        conversation persistence
    """

    question = question.strip()

    if not question:
        raise AppError("Question cannot be empty.")

    conversation = _get_or_create_conversation(
        store=store,
        conversation_id=conversation_id,
    )

    context = _get_context(conversation)

    previous_messages = store.get_messages(
        conversation_id=conversation["id"],
        limit=20,
    )

    plan = build_plan(
        question=question,
        context=context,
        messages=previous_messages,
        store=store,
        gemini=gemini,
        settings=settings,
    )

    _save_message(
        store=store,
        conversation_id=conversation["id"],
        role="user",
        content=question,
    )

    if plan.out_of_scope:
        answer = OUT_OF_SCOPE_ANSWER

        _save_message(
            store=store,
            conversation_id=conversation["id"],
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation["id"],
            context=context,
            plan=plan,
            answer=answer,
            evidence=[],
            analysis=None,
        )

        return ChatResponse(
            conversation_id=conversation["id"],
            answer=answer,
            status=ChatStatus.OUT_OF_SCOPE,
            sources=[],
            analysis=None,
            plan=plan,
        )

    if plan.ambiguous:
        answer = plan.ambiguity_reason or (
            "I need a little more information to determine which document "
            "or data you mean."
        )

        _save_message(
            store=store,
            conversation_id=conversation["id"],
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation["id"],
            context=context,
            plan=plan,
            answer=answer,
            evidence=[],
            analysis=None,
        )

        return ChatResponse(
            conversation_id=conversation["id"],
            answer=answer,
            status=ChatStatus.AMBIGUOUS,
            sources=[],
            analysis=None,
            plan=plan,
        )

    effective_question = (
        plan.resolved_question.strip()
        if plan.resolved_question and plan.resolved_question.strip()
        else question
    )

    evidence: list[Evidence] = []
    analysis: AnalysisResult | None = None

    try:
        if _needs_retrieval(plan):
            evidence = retrieve_evidence(
                plan=plan,
                question=effective_question,
                store=store,
                gemini=gemini,
                settings=settings,
            )

        if needs_analysis(plan):
            analysis = run_analysis(
                plan=plan,
                store=store,
                gemini=gemini,
                settings=settings,
            )

    except AnalysisError:
        raise
    except Exception as exc:
        log.exception("question_processing_failed")
        raise AppError(
            "The question could not be processed."
        ) from exc

    conflict = detect_conflicts(evidence)

    if conflict:
        answer = conflict

        _save_message(
            store=store,
            conversation_id=conversation["id"],
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation["id"],
            context=context,
            plan=plan,
            answer=answer,
            evidence=evidence,
            analysis=analysis,
        )

        return ChatResponse(
            conversation_id=conversation["id"],
            answer=answer,
            status=ChatStatus.CONFLICTING_INFORMATION,
            sources=sources_from_evidence(evidence),
            analysis=analysis,
            plan=plan,
        )

    evidence_valid = validate_evidence(
        question=effective_question,
        evidence=evidence,
        analysis=analysis,
        plan=plan,
    )

    if not evidence_valid:
        answer = _missing_information_answer(
            plan=plan,
            evidence=evidence,
            analysis=analysis,
        )

        _save_message(
            store=store,
            conversation_id=conversation["id"],
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation["id"],
            context=context,
            plan=plan,
            answer=answer,
            evidence=evidence,
            analysis=analysis,
        )

        return ChatResponse(
            conversation_id=conversation["id"],
            answer=answer,
            status=ChatStatus.MISSING_INFORMATION,
            sources=sources_from_evidence(evidence),
            analysis=analysis,
            plan=plan,
        )

    answer = _generate_answer(
        question=effective_question,
        plan=plan,
        evidence=evidence,
        analysis=analysis,
        gemini=gemini,
    )

    _save_message(
        store=store,
        conversation_id=conversation["id"],
        role="assistant",
        content=answer,
    )

    _update_context(
        store=store,
        conversation_id=conversation["id"],
        context=context,
        plan=plan,
        answer=answer,
        evidence=evidence,
        analysis=analysis,
    )

    return ChatResponse(
        conversation_id=conversation["id"],
        answer=answer,
        status=ChatStatus.ANSWERED,
        sources=sources_from_evidence(evidence),
        analysis=analysis,
        plan=plan,
    )


def _get_or_create_conversation(
    *,
    store: Storage,
    conversation_id: str | None,
) -> dict[str, Any]:
    if conversation_id:
        conversation = store.get_conversation(conversation_id)

        if conversation is not None:
            return conversation

    return store.create_conversation()


def _get_context(
    conversation: dict[str, Any],
) -> ConversationContext:
    raw_context = conversation.get("context")

    if isinstance(raw_context, ConversationContext):
        return raw_context

    if isinstance(raw_context, dict):
        try:
            return ConversationContext.model_validate(raw_context)
        except Exception:
            log.warning("invalid_conversation_context")

    return ConversationContext()


def _needs_retrieval(plan: QueryPlan) -> bool:
    """
    Decide whether semantic retrieval is required.

    Retrieval is used for PDF/TXT evidence. Structured CSV/Excel analysis is
    handled separately by analyze.py.
    """

    if not plan.operations:
        return True

    return any(
        operation.op == "retrieve"
        for operation in plan.operations
    )


def _generate_answer(
    *,
    question: str,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: AnalysisResult | None,
    gemini: GeminiService,
) -> str:
    prompt = _build_answer_prompt(
        question=question,
        plan=plan,
        evidence=evidence,
        analysis=analysis,
    )

    answer = gemini.generate(
        system_prompt=ANSWER_SYSTEM,
        user_prompt=prompt,
    )

    answer = answer.strip()

    if not answer:
        raise AppError("The model returned an empty answer.")

    return answer


def _build_answer_prompt(
    *,
    question: str,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: AnalysisResult | None,
) -> str:
    payload: dict[str, Any] = {
        "question": question,
        "plan": _plan_for_prompt(plan),
        "evidence": [
            _evidence_for_prompt(item)
            for item in evidence
        ],
        "analysis": (
            _analysis_for_prompt(analysis)
            if analysis is not None
            else None
        ),
    }

    return (
        "Answer the user's question using only the supplied information.\n\n"
        "USER QUESTION:\n"
        f"{question}\n\n"
        "SUPPLIED INFORMATION:\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str, indent=2)}\n\n"
        "Write the final answer for the user."
    )


def _plan_for_prompt(plan: QueryPlan) -> dict[str, Any]:
    return {
        "intent": plan.intent.value
        if hasattr(plan.intent, "value")
        else str(plan.intent),
        "is_follow_up": plan.is_follow_up,
        "resolved_question": plan.resolved_question,
        "entities": plan.entities,
        "metrics": plan.metrics,
        "years": plan.years,
        "filters": plan.filters,
        "document_hints": plan.document_hints,
        "operations": [
            operation.model_dump(
                by_alias=True,
                exclude_none=True,
            )
            for operation in plan.operations
        ],
    }


def _evidence_for_prompt(
    evidence: Evidence,
) -> dict[str, Any]:
    return {
        "document_id": evidence.document_id,
        "filename": evidence.filename,
        "document_type": evidence.document_type,
        "chunk_id": evidence.chunk_id,
        "text": evidence.text,
        "similarity": evidence.similarity,
        "page_number": evidence.page_number,
        "section": evidence.section,
        "source_reference": evidence.source_reference,
        "start_line": evidence.start_line,
        "end_line": evidence.end_line,
        "row_start": evidence.row_start,
        "row_end": evidence.row_end,
        "columns": evidence.columns,
        "entities": evidence.entities,
        "year": evidence.year,
    }


def _analysis_for_prompt(
    analysis: AnalysisResult,
) -> dict[str, Any]:
    return {
        "operation": analysis.operation,
        "value": analysis.value,
        "table": analysis.table,
        "inputs": analysis.inputs,
        "formula": analysis.formula,
        "source_file": analysis.source_file,
        "rows_used": analysis.rows_used,
        "columns_used": analysis.columns_used,
    }


def _missing_information_answer(
    *,
    plan: QueryPlan,
    evidence: list[Evidence],
    analysis: AnalysisResult | None,
) -> str:
    if analysis is None and not evidence:
        return MISSING_ANSWER

    if analysis is None:
        return (
            "I found related information in the uploaded documents, "
            "but it does not contain enough information to answer that "
            "question reliably."
        )

    return (
        "I could not obtain enough information from the uploaded data "
        "to answer that question reliably."
    )


def _save_message(
    *,
    store: Storage,
    conversation_id: str,
    role: str,
    content: str,
) -> None:
    store.add_message(
        conversation_id=conversation_id,
        role=role,
        content=content,
    )


def _update_context(
    *,
    store: Storage,
    conversation_id: str,
    context: ConversationContext,
    plan: QueryPlan,
    answer: str,
    evidence: list[Evidence],
    analysis: AnalysisResult | None,
) -> None:
    numeric_results: dict[str, Any] = {}

    if analysis is not None:
        numeric_results = {
            "operation": analysis.operation,
            "value": analysis.value,
            "source_file": analysis.source_file,
        }

    updated = context.model_copy(
        update={
            "last_intent": (
                plan.intent.value
                if hasattr(plan.intent, "value")
                else str(plan.intent)
            ),
            "last_question": plan.resolved_question or "",
            "last_answer": answer,
            "entities": list(plan.entities),
            "metrics": list(plan.metrics),
            "years": list(plan.years),
            "document_ids": list(plan.document_ids),
            "last_plan_summary": {
                "intent": (
                    plan.intent.value
                    if hasattr(plan.intent, "value")
                    else str(plan.intent)
                ),
                "operations": [
                    operation.model_dump(
                        by_alias=True,
                        exclude_none=True,
                    )
                    for operation in plan.operations
                ],
            },
            "last_numeric_results": numeric_results,
        }
    )

    store.update_conversation_context(
        conversation_id=conversation_id,
        context=updated.model_dump(),
    )
