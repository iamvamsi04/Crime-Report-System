"""Validation for files accepted by the local document collection."""

from __future__ import annotations

from pathlib import Path

from app.core.exceptions import FileValidationError
from app.core.models import DocumentType


SUPPORTED_EXTENSIONS: dict[str, DocumentType] = {
    ".pdf": DocumentType.PDF,
    ".txt": DocumentType.TXT,
    ".csv": DocumentType.CSV,
}


def validate_file(path: Path) -> DocumentType:
    """Return a supported type or raise a clear, user-safe validation error."""
    if not path.exists() or not path.is_file():
        raise FileValidationError(f"File not found: {path.name}")
    if path.stat().st_size == 0:
        raise FileValidationError(f"The file '{path.name}' is empty.")
    document_type = SUPPORTED_EXTENSIONS.get(path.suffix.lower())
    if document_type is None:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise FileValidationError(
            f"Unsupported file type '{path.suffix or '[no extension]'}' for '{path.name}'. "
            f"Supported types: {allowed}."
        )
    return document_type
