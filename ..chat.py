from __future__ import annotations

import json
import logging
from typing import Any

from app.analyze import needs_analysis, run_analysis
from app.errors import NotFoundError
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
    ConversationOut,
    MessageOut,
    QueryPlan,
    Source,
)
from app.plan import build_plan
from app.retrieve import retrieve_evidence
from app.storage import Storage

log = logging.getLogger(__name__)


ANSWER_SYSTEM = """
You are an e-commerce/document analysis assistant.

Answer the user's question ONLY from the evidence and analysis results
provided in the prompt.

Rules:

1. Do not use outside knowledge.
2. Do not invent facts, numbers, dates, entities, or sources.
3. For CSV/Excel questions, use calculated analysis results directly.
4. Do not redo calculations yourself when an analysis result is provided.
5. If the supplied evidence does not contain enough information, clearly say
   that the information is not available.
6. Keep the answer concise but explain the calculation when useful.
7. When sources are provided, identify the relevant source naturally.
8. Do not mention internal implementation details such as SQL, DuckDB,
   embeddings, vector databases, or planning unless the user explicitly asks.
"""


def ask(
    question: str,
    conversation_id: str | None,
    store: Storage,
    gemini: GeminiService,
    settings: Any,
) -> ChatResponse:
    """
    Process one user question.

    The overall conversation architecture remains unchanged:

        question
          ↓
        conversation/history
          ↓
        plan
          ↓
        retrieval and/or CSV analysis
          ↓
        evidence validation
          ↓
        Gemini answer
          ↓
        conversation context
    """

    question = question.strip()

    if not question:
        raise ValueError("Question cannot be empty.")

    conversation = _get_or_create_conversation(
        conversation_id=conversation_id,
        store=store,
    )

    conversation_id = conversation["id"]

    previous_messages = store.list_messages(
        conversation_id
    )

    previous_context = _get_conversation_context(
        conversation
    )

    plan = build_plan(
        question=question,
        conversation=conversation,
        messages=previous_messages,
        store=store,
        gemini=gemini,
    )

    effective_question = (
        plan.resolved_question
        or question
    )

    user_message = MessageOut(
        role="user",
        content=question,
    )

    store.insert_message(
        conversation_id=conversation_id,
        role="user",
        content=question,
    )

    # --------------------------------------------------------
    # History / repeat handling
    # --------------------------------------------------------

    if plan.history_target != "none":
        answer = _answer_from_history(
            plan=plan,
            messages=previous_messages,
            conversation=conversation,
        )

        store.insert_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.OK,
            sources=[],
            query_plan=plan.model_dump(
                by_alias=True
            ),
        )

    # --------------------------------------------------------
    # Out-of-scope
    # --------------------------------------------------------

    if plan.out_of_scope:
        answer = OUT_OF_SCOPE_ANSWER

        store.insert_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation_id,
            plan=plan,
            question=question,
            answer=answer,
            sources=[],
            analysis=[],
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.OUT_OF_SCOPE,
            sources=[],
            query_plan=plan.model_dump(
                by_alias=True
            ),
        )

    # --------------------------------------------------------
    # Ambiguous question
    # --------------------------------------------------------

    if plan.ambiguous:
        answer = plan.ambiguity_reason or (
            "I need a little more information to answer that accurately."
        )

        store.insert_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation_id,
            plan=plan,
            question=question,
            answer=answer,
            sources=[],
            analysis=[],
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.AMBIGUOUS,
            sources=[],
            query_plan=plan.model_dump(
                by_alias=True
            ),
        )

    # --------------------------------------------------------
    # Execution
    # --------------------------------------------------------

    execution_flow: list[str] = []

    evidence = []
    analysis: list[AnalysisResult] = []

    # --------------------------------------------------------
    # RAG retrieval
    # --------------------------------------------------------

    if any(
        operation.op == "retrieve"
        for operation in plan.operations
    ):
        execution_flow.append(
            "Searching documents"
        )

        evidence = retrieve_evidence(
            question=effective_question,
            plan=plan,
            store=store,
        )

    # --------------------------------------------------------
    # DuckDB CSV/Excel analysis
    # --------------------------------------------------------

    if needs_analysis(plan):
        execution_flow.append(
            "Analyzing dataset"
        )

        analysis = run_analysis(
            plan=plan,
            store=store,
            gemini=gemini,
            settings=settings,
        )

    # --------------------------------------------------------
    # Evidence validation
    # --------------------------------------------------------

    valid_evidence = validate_evidence(
        evidence=evidence,
        analysis=analysis,
    )

    conflicts = detect_conflicts(
        evidence=evidence,
        analysis=analysis,
    )

    sources = sources_from_evidence(
        evidence=evidence,
        analysis=analysis,
    )

    # --------------------------------------------------------
    # Missing information
    # --------------------------------------------------------

    if not valid_evidence and not analysis:
        answer = MISSING_ANSWER

        store.insert_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation_id,
            plan=plan,
            question=question,
            answer=answer,
            sources=sources,
            analysis=analysis,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.MISSING_INFORMATION,
            sources=sources,
            query_plan=plan.model_dump(
                by_alias=True
            ),
        )

    # --------------------------------------------------------
    # Conflicting information
    # --------------------------------------------------------

    if conflicts:
        answer = _generate_answer(
            question=effective_question,
            evidence=evidence,
            analysis=analysis,
            sources=sources,
            gemini=gemini,
            conflicts=conflicts,
        )

        store.insert_message(
            conversation_id=conversation_id,
            role="assistant",
            content=answer,
        )

        _update_context(
            store=store,
            conversation_id=conversation_id,
            plan=plan,
            question=question,
            answer=answer,
            sources=sources,
            analysis=analysis,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.CONFLICTING_INFORMATION,
            sources=sources,
            query_plan=plan.model_dump(
                by_alias=True
            ),
        )

    # --------------------------------------------------------
    # Normal answer
    # --------------------------------------------------------

    answer = _generate_answer(
        question=effective_question,
        evidence=evidence,
        analysis=analysis,
        sources=sources,
        gemini=gemini,
        conflicts=[],
    )

    store.insert_message(
        conversation_id=conversation_id,
        role="assistant",
        content=answer,
    )

    _update_context(
        store=store,
        conversation_id=conversation_id,
        plan=plan,
        question=question,
        answer=answer,
        sources=sources,
        analysis=analysis,
    )

    return ChatResponse(
        conversation_id=conversation_id,
        answer=answer,
        status=ChatStatus.OK,
        sources=sources,
        query_plan=plan.model_dump(
            by_alias=True
        ),
    )


# ============================================================
# CONVERSATION
# ============================================================


def _get_or_create_conversation(
    *,
    conversation_id: str | None,
    store: Storage,
) -> dict[str, Any]:
    if conversation_id:
        conversation = store.get_conversation(
            conversation_id
        )

        if conversation:
            return conversation

        raise NotFoundError(
            "Conversation not found."
        )

    return store.create_conversation()


def _get_conversation_context(
    conversation: dict[str, Any],
) -> ConversationContext | None:
    raw = conversation.get("context")

    if not raw:
        return None

    if isinstance(raw, ConversationContext):
        return raw

    if isinstance(raw, dict):
        try:
            return ConversationContext.model_validate(
                raw
            )
        except Exception:
            log.warning(
                "invalid_conversation_context"
            )
            return None

    if isinstance(raw, str):
        try:
            return ConversationContext.model_validate_json(
                raw
            )
        except Exception:
            log.warning(
                "invalid_conversation_context_json"
            )

    return None


# ============================================================
# HISTORY
# ============================================================


def _answer_from_history(
    *,
    plan: QueryPlan,
    messages: list[dict[str, Any]],
    conversation: dict[str, Any],
) -> str:
    """
    Handle history-oriented requests without invoking RAG or
    dataset analysis.

    This intentionally preserves the existing conversation
    behavior rather than sending history questions through the
    CSV analysis engine.
    """

    target = plan.history_target

    if target == "last_question":
        for message in reversed(messages):
            if message.get("role") == "user":
                return message.get(
                    "content",
                    "",
                )

    if target == "last_answer":
        for message in reversed(messages):
            if message.get("role") == "assistant":
                return message.get(
                    "content",
                    "",
                )

    if target == "conversation":
        parts: list[str] = []

        for message in messages:
            role = message.get("role")
            content = message.get("content")

            if role and content:
                parts.append(
                    f"{role}: {content}"
                )

        return "\n".join(parts)

    return (
        "I don't have enough previous conversation context "
        "to answer that."
    )


# ============================================================
# ANSWER GENERATION
# ============================================================


def _generate_answer(
    *,
    question: str,
    evidence: list[Any],
    analysis: list[AnalysisResult],
    sources: list[Source],
    gemini: GeminiService,
    conflicts: list[Any],
) -> str:
    evidence_payload = [
        _model_dump(item)
        for item in evidence
    ]

    analysis_payload = [
        result.model_dump(
            by_alias=True
        )
        for result in analysis
    ]

    source_payload = [
        _model_dump(source)
        for source in sources
    ]

    prompt = {
        "question": question,
        "evidence": evidence_payload,
        "analysis": analysis_payload,
        "sources": source_payload,
        "conflicts": [
            _model_dump(item)
            for item in conflicts
        ],
    }

    answer = gemini.generate_text(
        ANSWER_SYSTEM,
        json.dumps(
            prompt,
            default=str,
        ),
    )

    answer = answer.strip()

    if not answer:
        return MISSING_ANSWER

    return answer


# ============================================================
# CONTEXT UPDATE
# ============================================================


def _update_context(
    *,
    store: Storage,
    conversation_id: str,
    plan: QueryPlan,
    question: str,
    answer: str,
    sources: list[Source],
    analysis: list[AnalysisResult],
) -> None:
    """
    Persist the structured conversation state used by follow-up
    questions.

    The existing architecture is preserved.
    """

    numeric_results: list[Any] = []

    for result in analysis:
        if result.value is not None:
            numeric_results.append(
                result.value
            )

    context = ConversationContext(
        intent=plan.intent.value,
        question=question,
        answer=answer,
        entities=plan.entities,
        metrics=plan.metrics,
        years=plan.years,
        document_ids=plan.document_ids,
        plan_summary=_plan_summary(plan),
        numeric_results=numeric_results,
    )

    store.update_conversation(
        conversation_id,
        context=json.dumps(
            context.model_dump(
                by_alias=True
            ),
            default=str,
        ),
    )


def _plan_summary(
    plan: QueryPlan,
) -> str:
    operations = [
        operation.op
        for operation in plan.operations
    ]

    if not operations:
        return plan.intent.value

    return (
        f"{plan.intent.value}: "
        + ", ".join(operations)
    )


# ============================================================
# HELPERS
# ============================================================


def _model_dump(
    value: Any,
) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(
            by_alias=True
        )

    if isinstance(value, dict):
        return value

    return value
