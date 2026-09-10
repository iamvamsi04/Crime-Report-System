"""Explicit, local verification of ChromaDB and the all-MiniLM-L6-v2 embedding model."""

from __future__ import annotations

import sys

from app.core.config import settings
from app.core.exceptions import DocumentAnalysisError
from app.core.logging import configure_logging
from app.core.runtime import validate_backend_dependencies, validate_ui_dependency
from app.retrieval.vector_store import LocalVectorStore


def main() -> None:
    """Load the real local vector store and embedding model; model download may occur on first run."""
    configure_logging(settings.log_level)
    try:
        validate_backend_dependencies()
        validate_ui_dependency()
        store = LocalVectorStore(settings)
        embedding = store.embedding_function(["local runtime verification"])
        if not embedding or not embedding[0]:
            raise DocumentAnalysisError("all-MiniLM-L6-v2 returned no embedding values.")
    except DocumentAnalysisError as exc:
        print(f"Runtime verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(
        "Runtime verification passed: ChromaDB is local and cosine-configured; "
        "all-MiniLM-L6-v2 produced a local embedding."
    )


if __name__ == "__main__":
    main()
