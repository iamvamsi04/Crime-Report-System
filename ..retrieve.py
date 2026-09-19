from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd

from app.config import Settings
from app.errors import StorageError
from app.gemini import GeminiService
from app.models import Evidence, Intent, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)


def retrieve_evidence(
    *,
    plan: QueryPlan,
    question: str,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> list[Evidence]:
    query = _retrieval_query(plan, question)
    log.info("retrieval query_len=%s intent=%s", len(query), plan.intent)
    document_ids = list(plan.document_ids)
    if not document_ids and plan.document_hints:
        document_ids = _ids_for_hints(store, plan.document_hints)

    # Name lookups need original table rows, which summary embeddings can omit.
    if plan.intent == Intent.FACTUAL_LOOKUP and plan.entities:
        rows = _matching_table_rows(store, plan.entities, document_ids, settings.retrieval_top_k)
        if rows:
            plan.document_ids = list(dict.fromkeys(row.document_id for row in rows))
            return rows

    embedding = gemini.embed_texts([query])[0]
    if len(document_ids) >= 2:
        merged: list[Evidence] = []
        per = max(settings.retrieval_top_k // len(document_ids), 3)
        for doc_id in document_ids:
            merged.extend(
                store.query_chunks(
                    embedding,
                    n_results=per,
                    where={"document_id": doc_id},
                )
            )
        hits = merged
    else:
        where = _where_filter(document_ids)
        hits = store.query_chunks(embedding, n_results=settings.retrieval_top_k, where=where)

    kept = [h for h in hits if h.similarity >= settings.retrieval_min_similarity]
    kept.sort(key=lambda item: item.similarity, reverse=True)
    unique: list[Evidence] = []
    seen: set[str] = set()
    for item in kept:
        key = f"{item.document_id}:{item.chunk_id}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    log.info("retrieval_hits raw=%s kept=%s", len(hits), len(unique))
    return unique[: settings.retrieval_top_k]


def _matching_table_rows(
    store: Storage, entities: list[str], document_ids: list[str], limit: int,
) -> list[Evidence]:
    names = [re.escape(entity.strip()) for entity in entities if entity.strip()]
    if not names:
        return []
    pattern = r"(?<!\w)(?:" + "|".join(names) + r")(?!\w)"
    hits: list[Evidence] = []
    for doc in store.list_documents(ready_only=True):
        if doc["file_type"] not in {"csv", "excel"} or (document_ids and doc["id"] not in document_ids):
            continue
        try:
            path = store.document_file(doc["id"])
            frame = pd.read_excel(path, sheet_name=0) if doc["file_type"] == "excel" else pd.read_csv(path)
        except Exception as exc:
            raise StorageError("The dataset could not be read for this lookup.") from exc
        matches = frame.astype(str).apply(lambda col: col.str.contains(pattern, case=False, na=False)).any(axis=1)
        for idx, row in frame[matches].iterrows():
            row_number = int(idx) + 2
            text = ", ".join(f"{column}={value}" for column, value in row.items() if pd.notna(value))
            hits.append(Evidence(
                document_id=doc["id"], filename=doc["filename"], document_type=doc["file_type"],
                chunk_id=f"row-{row_number}", text=text, similarity=1.0,
                source_reference=f"{doc['filename']}, rows {row_number}-{row_number}",
                row_start=row_number, row_end=row_number, columns=", ".join(map(str, frame.columns)),
                entities=", ".join(entities),
            ))
            if len(hits) >= limit:
                return hits
    return hits


def _retrieval_query(plan: QueryPlan, question: str) -> str:
    if plan.retrieval_query:
        return plan.retrieval_query
    parts = [question, plan.intent.value]
    parts.extend(plan.entities)
    parts.extend(plan.metrics)
    parts.extend(str(y) for y in plan.years)
    parts.extend(plan.document_hints)
    return " ".join(p for p in parts if p)


def _ids_for_hints(store: Storage, hints: list[str]) -> list[str]:
    docs = store.list_documents(ready_only=True)
    ids: list[str] = []
    lowered = [h.lower() for h in hints]
    for doc in docs:
        name = doc["filename"].lower()
        if any(h in name or name in h for h in lowered):
            ids.append(doc["id"])
    return ids


def _where_filter(document_ids: list[str]) -> dict[str, Any] | None:
    if len(document_ids) == 1:
        return {"document_id": document_ids[0]}
    if len(document_ids) > 1:
        return {"document_id": {"$in": document_ids}}
    return None

