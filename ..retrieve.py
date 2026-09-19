from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.errors import RetrievalError
from app.gemini import GeminiService
from app.models import Evidence, QueryPlan
from app.storage import Storage


log = logging.getLogger(__name__)


STRUCTURED_TYPES = {
    "csv",
    "xlsx",
    "xls",
}

RAG_TYPES = {
    "pdf",
    "txt",
}


def retrieve_evidence(
    plan: QueryPlan,
    question: str,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> list[Evidence]:
    """
    Retrieve evidence for unstructured documents using semantic search.

    PDF/TXT:
        question
            -> embedding
            -> Chroma
            -> relevant chunks

    CSV/Excel:
        return no RAG evidence.

        Structured questions are handled by analyze.py through
        LLM-generated DuckDB SQL.

    This separation is intentional. We do not use vector similarity or
    Pandas row scanning as a substitute for SQL over structured data.
    """

    document_ids = _resolve_document_ids(
        plan=plan,
        store=store,
    )

    if document_ids:
        document_ids = _filter_to_rag_documents(
            document_ids,
            store,
        )

        if not document_ids:
            return []

    else:
        document_ids = _all_ready_rag_documents(
            store,
        )

        if not document_ids:
            return []

    retrieval_query = _retrieval_query(
        plan=plan,
        question=question,
    )

    if not retrieval_query.strip():
        return []

    try:
        embedding = gemini.embed_texts(
            [retrieval_query],
            task_type="RETRIEVAL_QUERY",
        )
    except Exception as exc:
        log.exception(
            "retrieval_embedding_failed"
        )
        raise RetrievalError(
            "Could not generate an embedding for the question."
        ) from exc

    if not embedding:
        raise RetrievalError(
            "The embedding service returned no query embedding."
        )

    query_embedding = embedding[0]

    per_document = _results_per_document(
        settings.retrieval_top_k,
        len(document_ids),
    )

    evidence: list[Evidence] = []

    for document_id in document_ids:
        try:
            matches = store.query_chunks(
                query_embedding,
                limit=per_document,
                where={
                    "document_id": document_id,
                },
            )
        except Exception as exc:
            log.exception(
                "chroma_query_failed document_id=%s",
                document_id,
            )
            raise RetrievalError(
                "Could not search the document index."
            ) from exc

        for match in matches:
            similarity = _normalize_similarity(
                match.similarity
            )

            if (
                similarity
                < settings.retrieval_min_similarity
            ):
                continue

            evidence.append(
                match.model_copy(
                    update={
                        "similarity": similarity,
                    }
                )
            )

    evidence.sort(
        key=lambda item: item.similarity,
        reverse=True,
    )

    evidence = _deduplicate(
        evidence
    )

    return evidence[
        : settings.retrieval_top_k
    ]


def _resolve_document_ids(
    plan: QueryPlan,
    store: Storage,
) -> list[str]:
    """
    Resolve documents explicitly selected by the planner.

    Document IDs are preferred because they are unambiguous.
    Filename hints are resolved against storage only when IDs are absent.
    """

    if plan.document_ids:
        return list(
            dict.fromkeys(
                plan.document_ids
            )
        )

    if not plan.document_hints:
        return []

    documents = store.list_documents()

    resolved: list[str] = []

    for hint in plan.document_hints:
        normalized_hint = (
            hint.strip().lower()
        )

        if not normalized_hint:
            continue

        for document in documents:
            if document.get(
                "status"
            ) != "ready":
                continue

            filename = str(
                document.get(
                    "filename",
                    "",
                )
            ).lower()

            if (
                normalized_hint in filename
            ):
                resolved.append(
                    str(
                        document["id"]
                    )
                )

    return list(
        dict.fromkeys(
            resolved
        )
    )


def _filter_to_rag_documents(
    document_ids: list[str],
    store: Storage,
) -> list[str]:
    """
    Keep only PDF/TXT documents.

    Structured documents are intentionally excluded because their data
    should be queried through DuckDB.
    """

    documents = store.list_documents()

    allowed: list[str] = []

    for document in documents:
        document_id = str(
            document.get("id")
        )

        if document_id not in document_ids:
            continue

        if document.get(
            "status"
        ) != "ready":
            continue

        file_type = str(
            document.get(
                "file_type",
                "",
            )
        ).lower()

        if file_type in RAG_TYPES:
            allowed.append(
                document_id
            )

    return list(
        dict.fromkeys(
            allowed
        )
    )


def _all_ready_rag_documents(
    store: Storage,
) -> list[str]:
    """
    Return all ready PDF/TXT documents.

    CSV/Excel are excluded.
    """

    documents = store.list_documents()

    result: list[str] = []

    for document in documents:
        if document.get(
            "status"
        ) != "ready":
            continue

        file_type = str(
            document.get(
                "file_type",
                "",
            )
        ).lower()

        if file_type in RAG_TYPES:
            result.append(
                str(
                    document["id"]
                )
            )

    return result


def _retrieval_query(
    plan: QueryPlan,
    question: str,
) -> str:
    """
    Build the semantic retrieval query.

    The original question remains the most important input. Planner
    information is appended only as disambiguating context.
    """

    if plan.retrieval_query:
        base = plan.retrieval_query.strip()
    elif plan.resolved_question:
        base = plan.resolved_question.strip()
    else:
        base = question.strip()

    if not base:
        return ""

    context: list[str] = []

    if plan.entities:
        context.append(
            "Entities: "
            + ", ".join(
                plan.entities
            )
        )

    if plan.metrics:
        context.append(
            "Metrics: "
            + ", ".join(
                plan.metrics
            )
        )

    if plan.years:
        context.append(
            "Years: "
            + ", ".join(
                str(year)
                for year in plan.years
            )
        )

    if plan.document_hints:
        context.append(
            "Documents: "
            + ", ".join(
                plan.document_hints
            )
        )

    if not context:
        return base

    return (
        f"{base}\n\n"
        "Additional retrieval context:\n"
        + "\n".join(context)
    )


def _results_per_document(
    total: int,
    document_count: int,
) -> int:
    """
    Distribute retrieval capacity across multiple documents.

    At least three chunks are requested per selected document when
    possible, while keeping the total result count bounded.
    """

    if document_count <= 0:
        return max(
            1,
            total,
        )

    if total <= 0:
        return 1

    if document_count == 1:
        return total

    return max(
        3,
        (total + document_count - 1)
        // document_count,
    )


def _normalize_similarity(
    value: Any,
) -> float:
    try:
        similarity = float(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return 0.0

    # Chroma's cosine-distance conversion is handled by Storage.
    # Clamp here so malformed metadata cannot produce impossible
    # similarity values.
    return max(
        0.0,
        min(
            1.0,
            similarity,
        ),
    )


def _deduplicate(
    evidence: list[Evidence],
) -> list[Evidence]:
    """
    Remove duplicate chunks while preserving highest similarity.
    """

    seen: set[tuple[str, str]] = set()
    result: list[Evidence] = []

    for item in evidence:
        key = (
            item.document_id,
            item.chunk_id,
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result
