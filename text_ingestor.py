"""UTF-8 text-file extraction with line-aware source attribution."""

from __future__ import annotations

from pathlib import Path

from app.core.exceptions import FileValidationError
from app.core.models import DocumentRecord, DocumentStatus, DocumentType, TextChunk
from app.ingestion.chunker import chunks_from_lines


def ingest_text(path: Path, document_id: str) -> tuple[DocumentRecord, list[TextChunk]]:
    """Read a non-empty text document and create line-addressable chunks."""
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileValidationError(f"The text file '{path.name}' must be UTF-8 encoded.") from exc
    except OSError as exc:
        raise FileValidationError(f"Unable to read text file '{path.name}'.") from exc

    if not contents.strip():
        raise FileValidationError(f"The text file '{path.name}' contains no text.")
    lines = contents.splitlines()
    chunks = chunks_from_lines(document_id=document_id, filename=path.name, lines=lines)
    if not chunks:
        raise FileValidationError(f"The text file '{path.name}' contains no usable text.")
    return (
        DocumentRecord(
            document_id=document_id,
            filename=path.name,
            path=str(path),
            document_type=DocumentType.TXT,
            status=DocumentStatus.READY,
            line_count=len(lines),
        ),
        chunks,
    )
