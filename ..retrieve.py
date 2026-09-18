from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.gemini import GeminiService
from app.models import Evidence, ExecutionMode, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)


TEXT_DOCUMENT_TYPES = {
    "pdf",
    "txt",
}


def retrieve_evidence(
    *,
    plan: QueryPlan,
    question: str,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> list[Evidence]:
    """
    Retrieve semantic evidence from textual documents.

    Structured CSV/Excel analysis is intentionally NOT performed here.

    Architecture:

        PDF / TXT
            -> embeddings
            -> semantic retrieval
            -> Evidence

        CSV / Excel
            -> dataset.py
            -> analyze.py
            -> DuckDB

    Hybrid plans use both pipelines independently.
    """

    if plan.mode not in {
        ExecutionMode.RETRIEVAL,
        ExecutionMode.HYBRID,
    }:
        return []

    retrieval_query = (
        plan.retrieval_query
        or question
    ).strip()

    if not retrieval_query:
        return []

    documents = store.list_documents(
        ready_only=True
    )

    target_documents = _target_text_documents(
        plan=plan,
        documents=documents,
    )

    if not target_documents:
        return []

    try:
        vectors = gemini.embed_texts(
            [retrieval_query]
        )
    except Exception:
        log.exception(
            "retrieval_query_embedding_failed"
        )
        return []

    if not vectors:
        return []

    query_embedding = vectors[0]

    top_k = max(
        1,
        int(
            getattr(
                settings,
                "retrieval_top_k",
                8,
            )
        ),
    )

    min_similarity = float(
        getattr(
            settings,
            "retrieval_min_similarity",
            0.20,
        )
    )

    evidence = _query_documents(
        target_documents=target_documents,
        query_embedding=query_embedding,
        store=store,
        top_k=top_k,
    )

    evidence = [
        item
        for item in evidence
        if _passes_similarity(
            item,
            min_similarity,
        )
    ]

    evidence = _deduplicate(
        evidence
    )

    evidence.sort(
        key=_similarity_sort_key,
        reverse=True,
    )

    return evidence[:top_k]


def _query_documents(
    *,
    target_documents: list[dict[str, Any]],
    query_embedding: list[float],
    store: Storage,
    top_k: int,
) -> list[Evidence]:
    """
    Retrieve per document.

    Per-document retrieval is preferable when several documents are selected:
    a single globally ranked query can otherwise allow one document to consume
    every result slot and hide relevant evidence from another selected file.
    """

    if not target_documents:
        return []

    if len(target_documents) == 1:
        document_id = str(
            target_documents[0]["id"]
        )

        return store.query_chunks(
            query_embedding=query_embedding,
            top_k=top_k,
            document_ids=[
                document_id
            ],
        )

    per_document_k = max(
        2,
        top_k,
    )

    combined: list[Evidence] = []

    for document in target_documents:
        document_id = str(
            document["id"]
        )

        try:
            matches = store.query_chunks(
                query_embedding=query_embedding,
                top_k=per_document_k,
                document_ids=[
                    document_id
                ],
            )

            combined.extend(matches)

        except Exception:
            log.exception(
                "document_retrieval_failed document_id=%s",
                document_id,
            )

    return combined


def _target_text_documents(
    *,
    plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Resolve which PDF/TXT files retrieval may search.

    Explicit planner-selected document IDs have priority.

    For a hybrid plan, structured document IDs are simply ignored here;
    analyze.py consumes those separately.
    """

    text_documents = [
        document
        for document in documents
        if _document_type(document)
        in TEXT_DOCUMENT_TYPES
    ]

    if not text_documents:
        return []

    by_id = {
        str(document["id"]): document
        for document in text_documents
    }

    selected: list[dict[str, Any]] = []

    for document_id in plan.document_ids:
        document = by_id.get(
            str(document_id)
        )

        if (
            document is not None
            and document not in selected
        ):
            selected.append(document)

    # Filename hints are useful for planner outputs and follow-up context.
    for hint in plan.document_hints:
        for document in text_documents:
            if not _filename_matches(
                filename=str(
                    document["filename"]
                ),
                hint=hint,
            ):
                continue

            if document not in selected:
                selected.append(document)

    if selected:
        return selected

    # If the planner did not identify a specific textual document, retrieval
    # can search the available textual corpus. This is appropriate for broad
    # questions such as "What do the reports say about supply-chain risk?"
    return text_documents


def _passes_similarity(
    evidence: Evidence,
    minimum: float,
) -> bool:
    """
    Similarity may be absent for some storage implementations. Such evidence
    should not be discarded merely because no score was attached.
    """

    if evidence.similarity is None:
        return True

    try:
        return float(
            evidence.similarity
        ) >= minimum
    except (TypeError, ValueError):
        return False


def _deduplicate(
    evidence: list[Evidence],
) -> list[Evidence]:
    """
    Deduplicate overlapping/repeated retrieval results.

    Prefer chunk IDs when available. Otherwise use document/source/text
    identity.
    """

    result: list[Evidence] = []
    seen: set[tuple[Any, ...]] = set()

    for item in evidence:
        if item.chunk_id:
            key: tuple[Any, ...] = (
                "chunk",
                item.document_id,
                item.chunk_id,
            )
        else:
            key = (
                "content",
                item.document_id,
                item.source_reference,
                item.page_number,
                item.start_line,
                item.end_line,
                item.text,
            )

        if key in seen:
            continue

        seen.add(key)
        result.append(item)

    return result


def _similarity_sort_key(
    evidence: Evidence,
) -> float:
    if evidence.similarity is None:
        return 0.0

    try:
        return float(
            evidence.similarity
        )
    except (TypeError, ValueError):
        return 0.0


def _filename_matches(
    *,
    filename: str,
    hint: str,
) -> bool:
    filename_normalized = (
        filename.strip().casefold()
    )

    hint_normalized = (
        hint.strip().casefold()
    )

    if (
        not filename_normalized
        or not hint_normalized
    ):
        return False

    if filename_normalized == hint_normalized:
        return True

    filename_stem = (
        filename_normalized.rsplit(
            ".",
            1,
        )[0]
    )

    hint_stem = (
        hint_normalized.rsplit(
            ".",
            1,
        )[0]
    )

    return (
        filename_stem == hint_stem
        or hint_normalized
        in filename_normalized
    )


def _document_type(
    document: dict[str, Any],
) -> str:
    file_type = str(
        document.get(
            "file_type"
        )
        or ""
    ).strip().lower().lstrip(".")

    if file_type:
        return file_type

    filename = str(
        document.get(
            "filename"
        )
        or ""
    ).lower()

    if "." not in filename:
        return ""

    return filename.rsplit(
        ".",
        1,
    )[-1]
