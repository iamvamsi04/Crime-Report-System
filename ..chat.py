from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

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


ANSWER_SYSTEM = """You answer questions using ONLY the supplied document evidence
and CSV/Excel analysis results.

Rules:
- Do not invent facts.
- Do not use outside knowledge.
- If the evidence does not support the answer, say that the information
  was not found in the available documents.
- If analysis results are supplied, use those calculated results directly.
- Do not redo arithmetic yourself when a calculated analysis result is provided.
- Be concise but explain the result clearly.
- When useful, mention the source filename.
- If the supplied evidence contains genuinely conflicting values, explicitly
  explain the conflict and identify which source reports each value.
"""


def ask(
    *,
    question: str,
    conversation_id: str | None,
    store: Storage,
    gemini: GeminiService,
    settings: Any,
) -> ChatResponse:
    question = question.strip()

    if not question:
        raise ValueError("Question cannot be empty.")

    conversation_id, context = _get_or_create_conversation(
        store,
        conversation_id,
    )

    # Read messages BEFORE saving the current question.
    # This makes "previous question" unambiguous.
    previous_messages = store.list_messages(conversation_id)

    plan = build_plan(
        question=question,
        context=context,
        documents=store.list_documents(ready_only=True),
        gemini=gemini,
    )

    # Store the current user question after planning.
    _save_user_message(
        store=store,
        conversation_id=conversation_id,
        question=question,
    )

    # ---------------------------------------------------------
    # Conversation history / repeat requests
    # ---------------------------------------------------------

    if plan.conversation_intent == "history":
        answer, sources, execution_flow = _handle_history_request(
            plan=plan,
            previous_messages=previous_messages,
        )

        # IMPORTANT:
        # Do not overwrite the substantive context with this
        # meta-question.
        _save_assistant_message(
            store=store,
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.ANSWERED,
            sources=sources,
            execution_flow=execution_flow,
            plan=plan,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.ANSWERED,
            sources=sources,
            execution_flow=execution_flow,
            query_plan=plan.model_dump(by_alias=True),
        )

    if plan.conversation_intent == "repeat":
        answer, sources, execution_flow, previous_plan = _handle_repeat_request(
            previous_messages=previous_messages,
            context=context,
        )

        if previous_plan is not None:
            response_plan = previous_plan
        else:
            response_plan = plan

        _save_assistant_message(
            store=store,
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.ANSWERED,
            sources=sources,
            execution_flow=execution_flow,
            plan=response_plan,
        )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            status=ChatStatus.ANSWERED,
            sources=sources,
            execution_flow=execution_flow,
            query_plan=response_plan.model_dump(by_alias=True),
        )

    # ---------------------------------------------------------
    # Normal document / CSV question
    # ---------------------------------------------------------

    effective_question = (
        plan.resolved_question.strip()
        if plan.conversation_intent == "follow_up"
        and plan.resolved_question
        else question
    )

    execution_flow: list[str] = [
        "Understanding question",
    ]

    evidence = []
    analysis: list[AnalysisResult] = []

    # Retrieval
    if any(op.op == "retrieve" for op in plan.operations):
        execution_flow.append("Retrieving document evidence")

        evidence = retrieve_evidence(
            plan=plan,
            question=effective_question,
            store=store,
            gemini=gemini,
            settings=settings,
        )

    # CSV / Excel analysis
    if needs_analysis(plan):
        execution_flow.append("Analyzing dataset")

        analysis = run_analysis(
            plan=plan,
            store=store,
            gemini=gemini,
        )

    # ---------------------------------------------------------
    # Determine answer status
    # ---------------------------------------------------------

    if plan.out_of_scope:
        status = ChatStatus.OUT_OF_SCOPE
        answer = OUT_OF_SCOPE_ANSWER
        sources = sources_from_evidence(evidence, analysis)

    elif plan.ambiguous:
        status = ChatStatus.AMBIGUOUS
        answer = (
            plan.ambiguity_reason
            or "The question is ambiguous. Please provide more specific information."
        )
        sources = sources_from_evidence(evidence, analysis)

    elif not validate_evidence(evidence, analysis):
        status = ChatStatus.MISSING_INFORMATION
        answer = MISSING_ANSWER
        sources = sources_from_evidence(evidence, analysis)

    else:
        sources = sources_from_evidence(evidence, analysis)

        conflict = detect_conflicts(
            evidence,
            analysis,
        )

        if conflict.genuine:
            status = ChatStatus.CONFLICTING_INFORMATION
            execution_flow.append("Checking conflicting evidence")

            answer = _generate_answer(
                question=effective_question,
                evidence=evidence,
                analysis=analysis,
                conflict_explanation=conflict.explanation,
                gemini=gemini,
            )
        else:
            status = ChatStatus.ANSWERED

            answer = _generate_answer(
                question=effective_question,
                evidence=evidence,
                analysis=analysis,
                conflict_explanation=None,
                gemini=gemini,
            )

    execution_flow.append("Generating answer")

    # ---------------------------------------------------------
    # Save assistant response
    # ---------------------------------------------------------

    _save_assistant_message(
        store=store,
        conversation_id=conversation_id,
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        plan=plan,
    )

    # ---------------------------------------------------------
    # Update substantive conversation context
    # ---------------------------------------------------------

    new_context = _update_context(
        context=context,
        question=question,
        answer=answer,
        status=status,
        plan=plan,
        analysis=analysis,
    )

    store.update_conversation_context(
        conversation_id,
        new_context,
    )

    return ChatResponse(
        conversation_id=conversation_id,
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        query_plan=plan.model_dump(by_alias=True),
    )


def get_conversation(
    store: Storage,
    conversation_id: str,
) -> ConversationOut:
    record = store.get_conversation(conversation_id)

    context = ConversationContext.model_validate_json(
        record["context_json"]
    )

    raw_messages = store.list_messages(conversation_id)

    messages: list[MessageOut] = []

    for message in raw_messages:
        messages.append(
            _message_out(message)
        )

    return ConversationOut(
        conversation_id=conversation_id,
        context=context,
        messages=messages,
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


# ============================================================
# Conversation creation / loading
# ============================================================


def _get_or_create_conversation(
    store: Storage,
    conversation_id: str | None,
) -> tuple[str, ConversationContext]:
    if conversation_id:
        record = store.get_conversation(conversation_id)

        context = ConversationContext.model_validate_json(
            record["context_json"]
        )

        return conversation_id, context

    new_id = str(uuid4())

    record = store.create_conversation(new_id)

    context = ConversationContext.model_validate_json(
        record["context_json"]
    )

    return new_id, context


# ============================================================
# History handling
# ============================================================


def _handle_history_request(
    *,
    plan: QueryPlan,
    previous_messages: list[dict[str, Any]],
) -> tuple[str, list[Source], list[str]]:
    target = plan.history_target

    if target == "previous_question":
        previous_question = _find_previous_message(
            previous_messages,
            role="user",
        )

        if previous_question is None:
            return (
                "There is no previous question in this conversation.",
                [],
                ["Reading conversation history"],
            )

        return (
            f'Your previous question was: "{previous_question["content"]}"',
            [],
            ["Reading conversation history"],
        )

    if target == "previous_answer":
        previous_answer = _find_previous_message(
            previous_messages,
            role="assistant",
        )

        if previous_answer is None:
            return (
                "There is no previous answer in this conversation.",
                [],
                ["Reading conversation history"],
            )

        return (
            previous_answer["content"],
            _parse_sources(previous_answer.get("sources_json")),
            _parse_execution_flow(
                previous_answer.get("execution_flow_json")
            ),
        )

    if target == "conversation_summary":
        return (
            _build_conversation_summary(previous_messages),
            [],
            ["Reading conversation history"],
        )

    # Gemini classified it as history but did not provide a target.
    # Use the semantic plan rather than guessing from the wording.
    return (
        _build_conversation_summary(previous_messages),
        [],
        ["Reading conversation history"],
    )


def _handle_repeat_request(
    *,
    previous_messages: list[dict[str, Any]],
    context: ConversationContext,
) -> tuple[
    str,
    list[Source],
    list[str],
    QueryPlan | None,
]:
    previous_answer = _find_previous_message(
        previous_messages,
        role="assistant",
    )

    if previous_answer is not None:
        return (
            previous_answer["content"],
            _parse_sources(previous_answer.get("sources_json")),
            _parse_execution_flow(
                previous_answer.get("execution_flow_json")
            ),
            _parse_plan(
                previous_answer.get("query_plan_json")
            ),
        )

    if context.last_answer:
        return (
            context.last_answer,
            [],
            ["Reading conversation context"],
            None,
        )

    return (
        "There is no previous answer to repeat.",
        [],
        ["Reading conversation history"],
        None,
    )


def _find_previous_message(
    messages: list[dict[str, Any]],
    *,
    role: str,
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") == role:
            return message

    return None


def _build_conversation_summary(
    messages: list[dict[str, Any]],
) -> str:
    if not messages:
        return "There is no conversation history yet."

    substantive = [
        message
        for message in messages
        if message.get("role") in {"user", "assistant"}
    ]

    if not substantive:
        return "There is no conversation history yet."

    lines: list[str] = []

    for message in substantive:
        role = message.get("role")

        if role == "user":
            lines.append(
                f'User: {message.get("content", "")}'
            )
        else:
            lines.append(
                f'Assistant: {message.get("content", "")}'
            )

    return "Conversation history:\n" + "\n".join(lines)


# ============================================================
# Answer generation
# ============================================================


def _generate_answer(
    *,
    question: str,
    evidence: list[Any],
    analysis: list[AnalysisResult],
    conflict_explanation: str | None,
    gemini: GeminiService,
) -> str:
    evidence_payload = []

    for item in evidence:
        evidence_payload.append(
            {
                "filename": item.filename,
                "document_type": item.document_type,
                "source_reference": item.source_reference,
                "page_number": item.page_number,
                "section": item.section,
                "rows": (
                    f"{item.row_start}-{item.row_end}"
                    if item.row_start
                    else None
                ),
                "text": item.text,
            }
        )

    analysis_payload = [
        result.model_dump()
        for result in analysis
    ]

    user_payload = {
        "question": question,
        "evidence": evidence_payload,
        "analysis_results": analysis_payload,
        "conflict": conflict_explanation,
    }

    return gemini.generate_text(
        ANSWER_SYSTEM,
        json.dumps(
            user_payload,
            default=str,
        ),
    )


# ============================================================
# Message persistence
# ============================================================


def _save_user_message(
    *,
    store: Storage,
    conversation_id: str,
    question: str,
) -> None:
    store.add_message(
        {
            "id": str(uuid4()),
            "conversation_id": conversation_id,
            "role": "user",
            "content": question,
            "created_at": _utcnow(),
        }
    )


def _save_assistant_message(
    *,
    store: Storage,
    conversation_id: str,
    answer: str,
    status: ChatStatus,
    sources: list[Source],
    execution_flow: list[str],
    plan: QueryPlan,
) -> None:
    store.add_message(
        {
            "id": str(uuid4()),
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": answer,
            "status": status.value,
            "sources_json": json.dumps(
                [
                    source.model_dump()
                    for source in sources
                ],
                default=str,
            ),
            "execution_flow_json": json.dumps(
                execution_flow
            ),
            "query_plan_json": json.dumps(
                plan.model_dump(
                    by_alias=True
                ),
                default=str,
            ),
            "created_at": _utcnow(),
        }
    )


# ============================================================
# Context persistence
# ============================================================


def _update_context(
    *,
    context: ConversationContext,
    question: str,
    answer: str,
    status: ChatStatus,
    plan: QueryPlan,
    analysis: list[AnalysisResult],
) -> ConversationContext:
    """
    Update only substantive conversation state.

    History/repeat questions must NOT replace the previous
    substantive question and answer.
    """

    if plan.conversation_intent in {"history", "repeat"}:
        return context

    numeric_results: list[dict[str, Any]] = []

    for result in analysis:
        if result.value is not None or result.table is not None:
            numeric_results.append(
                result.model_dump(
                    by_alias=True
                )
            )

    return ConversationContext(
        last_intent=plan.intent.value,
        last_question=question,
        last_answer=answer,
        entities=list(plan.entities),
        metrics=list(plan.metrics),
        years=list(plan.years),
        document_ids=list(plan.document_ids),
        last_plan_summary=json.dumps(
            plan.model_dump(
                by_alias=True
            ),
            default=str,
        ),
        last_numeric_results=numeric_results,
    )


# ============================================================
# Stored-message parsing
# ============================================================


def _message_out(
    message: dict[str, Any],
) -> MessageOut:
    return MessageOut(
        id=message["id"],
        role=message["role"],
        content=message["content"],
        status=message.get("status"),
        sources=_parse_sources(
            message.get("sources_json")
        ) or None,
        execution_flow=_parse_execution_flow(
            message.get("execution_flow_json")
        ) or None,
        created_at=message["created_at"],
    )


def _parse_sources(
    raw: str | None,
) -> list[Source]:
    if not raw:
        return []

    try:
        data = json.loads(raw)

        if not isinstance(data, list):
            return []

        return [
            Source.model_validate(item)
            for item in data
            if isinstance(item, dict)
        ]

    except (json.JSONDecodeError, TypeError, ValueError):
        log.warning("invalid_sources_json")
        return []


def _parse_execution_flow(
    raw: str | None,
) -> list[str]:
    if not raw:
        return []

    try:
        data = json.loads(raw)

        if not isinstance(data, list):
            return []

        return [
            str(item)
            for item in data
        ]

    except (json.JSONDecodeError, TypeError, ValueError):
        log.warning("invalid_execution_flow_json")
        return []


def _parse_plan(
    raw: str | None,
) -> QueryPlan | None:
    if not raw:
        return None

    try:
        data = json.loads(raw)

        if not isinstance(data, dict):
            return None

        return QueryPlan.model_validate(data)

    except (json.JSONDecodeError, TypeError, ValueError):
        log.warning("invalid_query_plan_json")
        return None


# ============================================================
# Utilities
# ============================================================


def _utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
