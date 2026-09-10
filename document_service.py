"""Coordinates local validation, extraction, cataloging, and vector indexing."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Protocol

from app.core.models import DocumentRecord, DocumentType, TextChunk
from app.ingestion.catalog import DocumentCatalog
from app.ingestion.csv_ingestor import ingest_csv
from app.ingestion.pdf_ingestor import ingest_pdf
from app.ingestion.text_ingestor import ingest_text
from app.ingestion.validator import validate_file


logger = logging.getLogger(__name__)


class TextIndexer(Protocol):
    def add_chunks(self, chunks: list[TextChunk]) -> None: ...

    def add_csv_schema(self, record: DocumentRecord) -> None: ...


class DocumentService:
    """Ingest supported local files and index only their locally embedded text/schema."""

    def __init__(self, catalog: DocumentCatalog, indexer: TextIndexer) -> None:
        self.catalog = catalog
        self.indexer = indexer

    def ingest_path(self, path: Path, document_id: str | None = None) -> DocumentRecord:
        """Validate and ingest one path, surfacing errors without terminating the app."""
        document_type = validate_file(path)
        identifier = document_id or str(uuid.uuid4())
        if document_type is DocumentType.PDF:
            record, chunks = ingest_pdf(path, identifier)
            self.indexer.add_chunks(chunks)
        elif document_type is DocumentType.TXT:
            record, chunks = ingest_text(path, identifier)
            self.indexer.add_chunks(chunks)
        else:
            record, _ = ingest_csv(path, identifier)
            self.indexer.add_csv_schema(record)
        self.catalog.upsert(record)
        logger.info("Ingested %s document %s", record.document_type.value, record.filename)
        return record

    def ingest_directory(self, directory: Path) -> list[DocumentRecord]:
        """Ingest every top-level supported file, continuing after individual failures."""
        records: list[DocumentRecord] = []
        if not directory.exists():
            return records
        for path in sorted(candidate for candidate in directory.iterdir() if candidate.is_file()):
            try:
                records.append(self.ingest_path(path))
            except Exception as exc:  # Individual documents should not block the rest of a folder.
                logger.warning("Skipping %s: %s", path.name, exc)
        return records
