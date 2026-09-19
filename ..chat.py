from __future__ import annotations

import json
import logging
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
    Evidence,
    MessageOut,
    QueryPlan,
    Source,
)
from app.plan import build_plan
from app.retrieve import retrieve_evidence
from app.storage import Storage

log = logging.getLogger(__name__)


ANSWER_SYSTEM = """
You are the answer-generation component of an intelligent document
analysis system.

Answer ONLY from the supplied evidence and structured-data analysis
results.

Rules:
1. Do not use outside knowledge.
2. Do not invent facts, values, calculations, documents, or sources.
3. Use calculated analysis results directly. Do not recalculate them.
4. If the supplied evidence or analysis does not contain enough
   information, say that the information is not available.
5. Mention the relevant source filename when useful.
6. If sources contain conflicting values, explicitly explain the conflict.
7. Keep the answer concise and directly answer the user's question.
8. For SQL-backed analysis, treat the returned analysis result as the
   authoritative calculation performed against the uploaded dataset.
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

    context_record = _get_or_create_conversation(
        conversation_id=conversation_id,
        store=store,
    )

    context = ConversationContext.model_validate_json(
        context_record["context_json"]
    )

    previous_messages = store.list_messages(
        context_record["id"]
    )

    plan = build_plan(
        question,
        context,
        store.list_documents(ready_only=True),
        gemini,
    )

    store.add_message(
        {
            "id": _new_id(),
            "conversation_id": context_record["id"],
            "role": "user",
            "content": question,
            "created_at": _utcnow(),
        }
    )

    if plan.intent.value == "history":
        answer = _history_answer(previous_messages)

        return _finish_response(
            conversation_id=context_record["id"],
            answer=answer,
            plan=plan,
            sources=[],
            execution_flow=["Understanding question"],
            status=ChatStatus.ANSWERED,
            store=store,
            context=context,
            question=question,
        )

    if plan.intent.value == "repeat":
        answer = _repeat_answer(previous_messages)

        return _finish_response(
            conversation_id=context_record["id"],
            answer=answer,
            plan=plan,
            sources=[],
            execution_flow=["Understanding question"],
            status=ChatStatus.ANSWERED,
            store=store,
            context=context,
            question=question,
        )

    effective_question = (
        plan.resolved_question
        if plan.resolved_question
        else question
    )

    execution_flow: list[str] = [
        "Understanding question"
    ]

    evidence: list[Evidence] = []
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
            gemini=gemini,
            settings=settings,
        )

    if plan.out_of_scope:
        status = ChatStatus.OUT_OF_SCOPE
        answer = OUT_OF_SCOPE_ANSWER
        sources: list[Source] = []

    elif plan.ambiguous:
        status = ChatStatus.AMBIGUOUS
        answer = (
            plan.reason
            or "The question is ambiguous. Please provide more detail."
        )
        sources = []

    elif not validate_evidence(
        evidence,
        analysis,
    ):
        status = ChatStatus.MISSING_INFORMATION
        answer = MISSING_ANSWER
        sources = []

    else:
        sources = sources_from_evidence(
            evidence,
            analysis,
        )

        conflict = detect_conflicts(
            evidence,
            analysis,
        )

        if conflict.conflict:
            status = ChatStatus.CONFLICTING_INFORMATION
        else:
            status = ChatStatus.ANSWERED

        execution_flow.append(
            "Generating answer"
        )

        answer = _generate_answer(
            question=effective_question,
            evidence=evidence,
            analysis=analysis,
            conflict=conflict,
            gemini=gemini,
        )

    store.add_message(
        {
            "id": _new_id(),
            "conversation_id": context_record["id"],
            "role": "assistant",
            "content": answer,
            "status": status.value,
            "sources_json": json.dumps(
                [source.model_dump() for source in sources],
                ensure_ascii=False,
            ),
            "execution_flow_json": json.dumps(
                execution_flow,
                ensure_ascii=False,
            ),
            "query_plan_json": plan.model_dump_json(),
            "created_at": _utcnow(),
        }
    )

    _update_context(
        context=context,
        question=question,
        answer=answer,
        plan=plan,
        analysis=analysis,
    )

    store.update_conversation_context(
        context_record["id"],
        context,
    )

    return ChatResponse(
        conversation_id=context_record["id"],
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        query_plan=plan,
    )


def get_conversation(
    conversation_id: str,
    store: Storage,
) -> dict[str, Any]:
    record = store.get_conversation(
        conversation_id
    )

    context = ConversationContext.model_validate_json(
        record["context_json"]
    )

    messages = store.list_messages(
        conversation_id
    )

    return {
        "id": record["id"],
        "context": context,
        "messages": [
            MessageOut(
                id=message["id"],
                role=message["role"],
                content=message["content"],
                status=message.get("status"),
                sources=_parse_sources(
                    message.get("sources_json")
                ),
                execution_flow=_parse_execution_flow(
                    message.get("execution_flow_json")
                ),
                created_at=message["created_at"],
            )
            for message in messages
        ],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
    }


def _get_or_create_conversation(
    *,
    conversation_id: str | None,
    store: Storage,
) -> dict[str, Any]:
    if conversation_id:
        return store.get_conversation(
            conversation_id
        )

    return store.create_conversation(
        _new_id()
    )


def _generate_answer(
    *,
    question: str,
    evidence: list[Evidence],
    analysis: list[AnalysisResult],
    conflict: Any,
    gemini: GeminiService,
) -> str:
    evidence_payload = [
        {
            "filename": item.filename,
            "document_type": item.document_type,
            "source_reference": item.source_reference,
            "page": item.page_number,
            "section": item.section,
            "rows": (
                f"{item.row_start}-{item.row_end}"
                if item.row_start or item.row_end
                else None
            ),
            "text": item.text,
        }
        for item in evidence
    ]

    analysis_payload = [
        result.model_dump()
        for result in analysis
    ]

    user_payload = {
        "question": question,
        "evidence": evidence_payload,
        "analysis_results": analysis_payload,
        "conflict": (
            conflict.model_dump()
            if hasattr(conflict, "model_dump")
            else conflict
        ),
    }

    try:
        answer = gemini.generate_text(
            ANSWER_SYSTEM,
            json.dumps(
                user_payload,
                ensure_ascii=False,
            ),
        )
    except Exception:
        log.exception(
            "answer_generation_failed"
        )
        raise

    return answer.strip()


def _finish_response(
    *,
    conversation_id: str,
    answer: str,
    plan: QueryPlan,
    sources: list[Source],
    execution_flow: list[str],
    status: ChatStatus,
    store: Storage,
    context: ConversationContext,
    question: str,
) -> ChatResponse:
    store.add_message(
        {
            "id": _new_id(),
            "conversation_id": conversation_id,
            "role": "assistant",
            "content": answer,
            "status": status.value,
            "sources_json": json.dumps(
                [source.model_dump() for source in sources],
                ensure_ascii=False,
            ),
            "execution_flow_json": json.dumps(
                execution_flow,
                ensure_ascii=False,
            ),
            "query_plan_json": plan.model_dump_json(),
            "created_at": _utcnow(),
        }
    )

    _update_context(
        context=context,
        question=question,
        answer=answer,
        plan=plan,
        analysis=[],
    )

    store.update_conversation_context(
        conversation_id,
        context,
    )

    return ChatResponse(
        conversation_id=conversation_id,
        answer=answer,
        status=status,
        sources=sources,
        execution_flow=execution_flow,
        query_plan=plan,
    )


def _update_context(
    *,
    context: ConversationContext,
    question: str,
    answer: str,
    plan: QueryPlan,
    analysis: list[AnalysisResult],
) -> None:
    context.last_question = question
    context.last_answer = answer
    context.last_intent = plan.intent.value
    context.entities = list(plan.entities)
    context.metrics = list(plan.metrics)
    context.years = list(plan.years)
    context.document_ids = list(plan.document_ids)
    context.last_plan_summary = plan.model_dump()

    context.last_numeric_results = [
        {
            "operation": result.operation,
            "value": result.value,
            "source_file": result.source_file,
        }
        for result in analysis
        if result.value is not None
    ]


def _history_answer(
    messages: list[dict[str, Any]],
) -> str:
    user_messages = [
        message["content"]
        for message in messages
        if message.get("role") == "user"
    ]

    if not user_messages:
        return "There is no previous conversation yet."

    return "Previous questions:\n" + "\n".join(
        f"- {question}"
        for question in user_messages[-10:]
    )


def _repeat_answer(
    messages: list[dict[str, Any]],
) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return message["content"]

    return "There is no previous answer to repeat."


def _parse_sources(
    raw: str | None,
) -> list[dict[str, Any]]:
    if not raw:
        return []

    try:
        value = json.loads(raw)
    except Exception:
        return []

    return value if isinstance(value, list) else []


def _parse_execution_flow(
    raw: str | None,
) -> list[str]:
    if not raw:
        return []

    try:
        value = json.loads(raw)
    except Exception:
        return []

    return value if isinstance(value, list) else []


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _utcnow() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
