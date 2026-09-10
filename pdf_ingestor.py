"""Text-based PDF extraction using PyMuPDF; OCR is intentionally out of scope."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf

from app.core.exceptions import FileValidationError
from app.core.models import DocumentRecord, DocumentStatus, DocumentType, TextChunk
from app.ingestion.chunker import chunks_from_page


logger = logging.getLogger(__name__)


def ingest_pdf(path: Path, document_id: str) -> tuple[DocumentRecord, list[TextChunk]]:
    """Extract selectable PDF text page-by-page or explain why the PDF is unusable."""
    try:
        pdf = pymupdf.open(path)
    except (pymupdf.FileDataError, RuntimeError, OSError) as exc:
        raise FileValidationError(f"The PDF '{path.name}' is corrupt or unreadable.") from exc

    try:
        if pdf.page_count == 0:
            raise FileValidationError(f"The PDF '{path.name}' contains no pages.")
        chunks: list[TextChunk] = []
        extracted_characters = 0
        for page_index, page in enumerate(pdf, start=1):
            page_text = page.get_text("text").strip()
            extracted_characters += len(page_text)
            chunks.extend(
                chunks_from_page(
                    document_id=document_id,
                    filename=path.name,
                    page_number=page_index,
                    text=page_text,
                )
            )
        if extracted_characters < 20 or not chunks:
            raise FileValidationError(
                f"The PDF '{path.name}' has no extractable text. Scanned/image-only PDFs are unsupported."
            )
        return (
            DocumentRecord(
                document_id=document_id,
                filename=path.name,
                path=str(path),
                document_type=DocumentType.PDF,
                status=DocumentStatus.READY,
                page_count=pdf.page_count,
            ),
            chunks,
        )
    finally:
        pdf.close()
