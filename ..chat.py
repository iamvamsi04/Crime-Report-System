from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

from app.analyze import needs_analysis, run_analysis
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
You answer questions using ONLY the supplied document evidence
and CSV/Excel analysis results.

Rules:

- Do not invent facts.
- Do not use outside knowledge.
- If the supplied evidence does not support the answer, say that the
  information was not found in the uploaded documents.
- If analysis results are supplied, use those calculated results directly.
- Do not redo arithmetic yourself when a calculated analysis result is provided.
- Be concise but explain the result clearly.
- Mention the source filename when useful.
- If genuinely conflicting information is supplied, explain the conflict
  and identify the source associated with each value.
- Return JSON with exactly this structure:

{
    "answer": "your answer"
}
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
        store=store,
        conversation_id=conversation_id,
    )

    previous_messages = store.list_messages(
        conversation_id
    )

    documents = store.list_documents(
        ready_only=True
    )

    plan = build_plan(
        question=question,
        context=context,
        documents=documents,
        gemini=gemini,
    )

    _save_user_message(
        store=store,
        conversation_id=conversation_id,
        question=question,
    )

    if plan.conversation_intent == "history":
        answer, sources, execution_flow = _handle_history_request(
            plan=plan,
            previous_messages=previous_messages,
        )

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
            query_plan=plan.model_dump(
                by_alias=True
            ),
        )

    if plan.conversation_intent == "repeat":
        (
            answer,
            sources,
            execution_flow,
            previous_plan,
        ) = _handle_repeat_request(
            previous_messages=previous_messages,
            context=context,
        )

        response_plan = (
            previous_plan
            if previous_plan is not None
            else plan
        )

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
            query_plan=response_plan.model_dump(
                by_alias=True
            ),
        )

    effective_question = (
        plan.resolved_question.strip()
        if (
            plan.conversation_intent == "follow_up"
            and plan.resolved_question
        )
        else question
    )

    execution_flow: list[str] = [
        "Understanding question"
    ]

    evidence = []
    analysis: list[AnalysisResult] = []

    if any(
        operation.op == "retrieve"
        for operation in plan.operations
    ):
        execution_flow.append(
            "Retrieving document evidence"
        )

        evidence = retrieve_evidence(
            plan=plan,
            question=effective_question,
            store=store,
            gemini=gemini,
            settings=settings,
        )

    if needs_analysis(plan):
        execution_flow.append(
            "Analyzing dataset"
        )

        analysis = run_analysis(
            plan=plan,
            store=store,
        )

    sources = sources_from_evidence(
        evidence,
        analysis,
    )

    if plan.out_of_scope:
        status = ChatStatus.OUT_OF_SCOPE
        answer = OUT_OF_SCOPE_ANSWER

    elif plan.ambiguous:
        status = ChatStatus.AMBIGUOUS
        answer = (
            plan.ambiguity_reason
            or (
                "The question is ambiguous. "
                "Please provide more specific information."
            )
        )

    elif not validate_evidence(
        evidence,
        analysis,
    ):
        status = ChatStatus.MISSING_INFORMATION
        answer = MISSING_ANSWER

    else:
        conflict = detect_conflicts(
            evidence,
            analysis,
        )

        if conflict.genuine:
            status = ChatStatus.CONFLICTING_INFORMATION

            execution_flow.append(
                "Checking conflicting evidence"
            )

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

    execution_flow.append(
        "Generating answer"
    )

    _save_assistant_message(
        store=store,
        conversation_id=conversation_id,
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        plan=plan,
    )

    new_context = _update_context(
        context=context,
        question=question,
        answer=answer,
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
        query_plan=plan.model_dump(
            by_alias=True
        ),
    )


def get_conversation(
    *,
    store: Storage,
    conversation_id: str,
) -> ConversationOut:
    record = store.get_conversation(
        conversation_id
    )

    context = ConversationContext.model_validate_json(
        record["context_json"]
    )

    raw_messages = store.list_messages(
        conversation_id
    )

    messages = [
        _message_out(message)
        for message in raw_messages
    ]

    return ConversationOut(
        conversation_id=conversation_id,
        context=context,
        messages=messages,
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _get_or_create_conversation(
    *,
    store: Storage,
    conversation_id: str | None,
) -> tuple[str, ConversationContext]:
    if conversation_id:
        record = store.get_conversation(
            conversation_id
        )

        context = ConversationContext.model_validate_json(
            record["context_json"]
        )

        return conversation_id, context

    new_id = str(uuid4())

    record = store.create_conversation(
        new_id
    )

    context = ConversationContext.model_validate_json(
        record["context_json"]
    )

    return new_id, context


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
            (
                f'Your previous question was: '
                f'"{previous_question["content"]}"'
            ),
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
            _parse_sources(
                previous_answer.get("sources_json")
            ),
            _parse_execution_flow(
                previous_answer.get(
                    "execution_flow_json"
                )
            ),
        )

    return (
        _build_conversation_summary(
            previous_messages
        ),
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
            _parse_sources(
                previous_answer.get(
                    "sources_json"
                )
            ),
            _parse_execution_flow(
                previous_answer.get(
                    "execution_flow_json"
                )
            ),
            _parse_plan(
                previous_answer.get(
                    "query_plan_json"
                )
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
        return (
            "There is no conversation history yet."
        )

    lines: list[str] = []

    for message in messages:
        role = message.get("role")

        if role == "user":
            lines.append(
                f'User: {message.get("content", "")}'
            )

        elif role == "assistant":
            lines.append(
                f'Assistant: {message.get("content", "")}'
            )

    if not lines:
        return (
            "There is no conversation history yet."
        )

    return (
        "Conversation history:\n"
        + "\n".join(lines)
    )


def _generate_answer(
    *,
    question: str,
    evidence: list[Any],
    analysis: list[AnalysisResult],
    conflict_explanation: str | None,
    gemini: GeminiService,
) -> str:
    evidence_payload: list[dict[str, Any]] = []

    for item in evidence:
        evidence_payload.append(
            {
                "filename": item.filename,
                "document_type": item.document_type,
                "source_reference": item.source_reference,
                "page_number": item.page_number,
                "section": item.section,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "row_start": item.row_start,
                "row_end": item.row_end,
                "text": item.text,
            }
        )

    analysis_payload = [
        result.model_dump(
            by_alias=True
        )
        for result in analysis
    ]

    payload = {
        "question": question,
        "evidence": evidence_payload,
        "analysis_results": analysis_payload,
        "conflict": conflict_explanation,
    }

    response = gemini.generate_json(
        system=ANSWER_SYSTEM,
        user=json.dumps(
            payload,
            default=str,
        ),
    )

    answer = response.get("answer")

    if isinstance(answer, str):
        answer = answer.strip()

        if answer:
            return answer

    return MISSING_ANSWER


def _save_user_message(
    *,
    store: Storage,
    conversation_id: str,
    question: str,
) -> None:
    store.insert_message(
        {
            "message_id": str(uuid4()),
            "conversation_id": conversation_id,
            "role": "user",
            "content": question,
            "status": None,
            "sources_json": None,
            "execution_flow_json": None,
            "query_plan_json": None,
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
    store.insert_message(
        {
            "message_id": str(uuid4()),
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": answer,
            "status": status.value,
            "sources_json": json.dumps(
                [
                    source.model_dump(
                        by_alias=True
                    )
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


def _update_context(
    *,
    context: ConversationContext,
    question: str,
    answer: str,
    plan: QueryPlan,
    analysis: list[AnalysisResult],
) -> ConversationContext:
    if plan.conversation_intent in {
        "history",
        "repeat",
    }:
        return context

    return ConversationContext(
        last_question=question,
        last_answer=answer,
        last_intent=plan.intent.value,
        last_resolved_question=(
            plan.resolved_question
            or question
        ),
        entities=list(
            plan.entities
        ),
        metrics=list(
            plan.metrics
        ),
        years=list(
            plan.years
        ),
        filters=list(
            plan.filters
        ),
        document_ids=list(
            plan.document_ids
        ),
        document_hints=list(
            plan.document_hints
        ),
        operations=list(
            plan.operations
        ),
        last_analysis=[
            result
            for result in analysis
        ],
    )


def _message_out(
    message: dict[str, Any],
) -> MessageOut:
    return MessageOut(
        message_id=message["message_id"],
        role=message["role"],
        content=message["content"],
        status=message.get("status"),
        sources=_parse_sources(
            message.get("sources_json")
        ),
        execution_flow=_parse_execution_flow(
            message.get("execution_flow_json")
        ),
        query_plan=_parse_query_plan_dict(
            message.get("query_plan_json")
        ),
        created_at=message.get("created_at"),
    )


def _parse_sources(
    raw: str | None,
) -> list[Source]:
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        log.warning(
            "invalid_sources_json"
        )
        return []

    if not isinstance(data, list):
        return []

    sources: list[Source] = []

    for item in data:
        if not isinstance(item, dict):
            continue

        try:
            sources.append(
                Source.model_validate(item)
            )
        except ValueError:
            log.warning(
                "invalid_source_item"
            )

    return sources


def _parse_execution_flow(
    raw: str | None,
) -> list[str]:
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        log.warning(
            "invalid_execution_flow_json"
        )
        return []

    if not isinstance(data, list):
        return []

    return [
        str(item)
        for item in data
    ]


def _parse_plan(
    raw: str | None,
) -> QueryPlan | None:
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        log.warning(
            "invalid_query_plan_json"
        )
        return None

    if not isinstance(data, dict):
        return None

    try:
        return QueryPlan.model_validate(
            data
        )
    except ValueError:
        log.warning(
            "invalid_query_plan"
        )
        return None


def _parse_query_plan_dict(
    raw: str | None,
) -> dict[str, Any] | None:
    if not raw:
        return None

    try:
        data = json.loads(raw)
    except (
        json.JSONDecodeError,
        TypeError,
    ):
        return None

    if isinstance(data, dict):
        return data

    return None


def _utcnow() -> str:
    return (
        datetime.now(
            timezone.utc
        )
        .replace(
            microsecond=0
        )
        .isoformat()
    )
