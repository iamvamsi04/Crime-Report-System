"""Converts local Chroma cosine-distance matches into attributed evidence."""

from __future__ import annotations

from app.core.config import Settings
from app.core.models import DocumentType, EvidenceItem, SourceReference
from app.retrieval.vector_store import LocalVectorStore


class SemanticRetriever:
    """Retrieve only relevant local chunks and preserve their provenance."""

    def __init__(self, vector_store: LocalVectorStore, settings: Settings) -> None:
        self.vector_store = vector_store
        self.settings = settings

    def retrieve(
        self, query: str, document_ids: list[str] | None = None, limit: int | None = None
    ) -> list[EvidenceItem]:
        raw = self.vector_store.query_text(
            query=query,
            limit=limit or self.settings.retrieval_limit,
            document_ids=document_ids,
        )
        ids = self._first_result_batch(raw.get("ids"))
        documents = self._first_result_batch(raw.get("documents"))
        metadatas = self._first_result_batch(raw.get("metadatas"))
        distances = self._first_result_batch(raw.get("distances"))
        evidence: list[EvidenceItem] = []
        for chunk_id, text, metadata, distance in zip(ids, documents, metadatas, distances, strict=True):
            # Chroma cosine distance is 1 - cosine_similarity for normalized vectors.
            similarity = 1.0 - float(distance)
            if similarity < self.settings.retrieval_min_similarity:
                continue
            page_number = int(metadata.get("page_number", -1))
            line_start = int(metadata.get("line_start", -1))
            line_end = int(metadata.get("line_end", -1))
            evidence.append(
                EvidenceItem(
                    evidence_id=str(chunk_id),
                    text=str(text),
                    similarity=similarity,
                    source=SourceReference(
                        source_id=str(chunk_id),
                        filename=str(metadata["filename"]),
                        document_type=DocumentType(str(metadata["document_type"])),
                        excerpt=str(text)[:500],
                        page_number=page_number if page_number > 0 else None,
                        line_start=line_start if line_start > 0 else None,
                        line_end=line_end if line_end > 0 else None,
                        section=str(metadata.get("section") or "") or None,
                    ),
                )
            )
        return evidence

    @staticmethod
    def _first_result_batch(values):
        """Support both list and NumPy-array result batches returned by Chroma versions."""
        if values is None or len(values) == 0:
            return []
        return values[0]
