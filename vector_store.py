"""Persistent local ChromaDB store configured explicitly for cosine distance."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.core.exceptions import DocumentAnalysisError
from app.core.models import DocumentRecord, TextChunk


logger = logging.getLogger(__name__)


class LocalEmbeddingFunction:
    """Lazy, CPU-friendly local embedding function using all-MiniLM-L6-v2."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", cache_dir: Path | None = None) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: Any | None = None

    @property
    def model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise DocumentAnalysisError(
                    "sentence-transformers is not installed. Run 'pip install -r requirements.txt'."
                ) from exc
            try:
                self._model = SentenceTransformer(
                    self.model_name,
                    device="cpu",
                    cache_folder=str(self.cache_dir) if self.cache_dir else None,
                )
            except Exception as exc:
                cache_location = str(self.cache_dir) if self.cache_dir else "the default sentence-transformers cache"
                raise DocumentAnalysisError(
                    f"Unable to load the local embedding model '{self.model_name}'. "
                    f"Check network access for the initial download and write access to {cache_location}."
                ) from exc
        return self._model

    def __call__(self, input: list[str]) -> list[list[float]]:
        embeddings = self.model.encode(input, normalize_embeddings=True, show_progress_bar=False)
        return embeddings.tolist()

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """Use the same normalized local MiniLM embedding for ChromaDB query inputs."""
        return self(input)

    @staticmethod
    def name() -> str:
        """Stable ChromaDB 1.x identifier for this locally hosted embedding function."""
        return "local_sentence_transformers_minilm"

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> "LocalEmbeddingFunction":
        """Reconstruct the local-only embedding function from Chroma's persisted configuration."""
        cache_dir = config.get("cache_dir")
        return LocalEmbeddingFunction(
            model_name=str(config.get("model_name", "all-MiniLM-L6-v2")),
            cache_dir=Path(str(cache_dir)) if cache_dir else None,
        )

    def get_config(self) -> dict[str, Any]:
        """Provide ChromaDB 1.x a serializable configuration without loading the model."""
        return {
            "model_name": self.model_name,
            "device": "cpu",
            "normalize_embeddings": True,
            "cache_dir": str(self.cache_dir) if self.cache_dir else None,
        }

    def is_legacy(self) -> bool:
        """Declare the complete ChromaDB 1.x embedding-function configuration protocol."""
        return False

    def default_space(self) -> str:
        return "cosine"

    def supported_spaces(self) -> list[str]:
        return ["cosine", "l2", "ip"]


class LocalVectorStore:
    """Two local Chroma collections: text chunks and lightweight CSV schemas."""

    TEXT_COLLECTION = "document_text_chunks"
    CSV_COLLECTION = "csv_schemas"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_directories()
        try:
            import chromadb
        except ImportError as exc:
            raise DocumentAnalysisError(
                "chromadb is not installed. Run 'pip install -r requirements.txt'."
            ) from exc
        self.embedding_function = LocalEmbeddingFunction(settings.embedding_model, settings.embedding_cache_dir)
        self.client = chromadb.PersistentClient(path=str(settings.chroma_dir))
        cosine_configuration = {"hnsw": {"space": settings.chroma_distance_space}}
        self.text_collection = self.client.get_or_create_collection(
            name=self.TEXT_COLLECTION,
            configuration=cosine_configuration,
            embedding_function=self.embedding_function,
        )
        self.csv_collection = self.client.get_or_create_collection(
            name=self.CSV_COLLECTION,
            configuration=cosine_configuration,
            embedding_function=self.embedding_function,
        )
        self._verify_cosine_configuration(self.text_collection)
        self._verify_cosine_configuration(self.csv_collection)

    def _verify_cosine_configuration(self, collection: Any) -> None:
        configuration = getattr(collection, "configuration", None) or {}
        configured_distance = (configuration.get("hnsw") or {}).get("space")
        # Legacy persisted collections may still expose their distance setting
        # through metadata. New collections always use the configuration API above.
        if configured_distance is None:
            configured_distance = (getattr(collection, "metadata", None) or {}).get("hnsw:space")
        if configured_distance != "cosine":
            raise DocumentAnalysisError(
                f"Chroma collection '{collection.name}' must use cosine similarity; found '{configured_distance}'. "
                "Delete or re-create the local data/chroma directory before continuing."
            )

    def add_chunks(self, chunks: list[TextChunk]) -> None:
        if not chunks:
            return
        self.text_collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            metadatas=[
                {
                    "document_id": chunk.document_id,
                    "filename": chunk.filename,
                    "document_type": chunk.document_type.value,
                    "page_number": chunk.page_number or -1,
                    "line_start": chunk.line_start or -1,
                    "line_end": chunk.line_end or -1,
                    "section": chunk.section or "",
                }
                for chunk in chunks
            ],
        )

    def add_csv_schema(self, record: DocumentRecord) -> None:
        schema_text = (
            f"CSV dataset {record.filename}. Columns: {', '.join(record.columns)}. "
            f"Rows: {record.row_count or 0}."
        )
        self.csv_collection.upsert(
            ids=[f"schema-{record.document_id}"],
            documents=[schema_text],
            metadatas=[
                {
                    "document_id": record.document_id,
                    "filename": record.filename,
                    "document_type": "csv",
                }
            ],
        )

    def query_text(
        self, query: str, limit: int, document_ids: list[str] | None = None
    ) -> dict[str, Any]:
        where = {"document_id": {"$in": document_ids}} if document_ids else None
        return self.text_collection.query(
            query_texts=[query],
            n_results=limit,
            where=where,
            include=["documents", "metadatas", "distances"],
        )

    def query_csv_schemas(self, query: str, limit: int = 5) -> dict[str, Any]:
        return self.csv_collection.query(
            query_texts=[query], n_results=limit, include=["documents", "metadatas", "distances"]
        )
