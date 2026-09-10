"""Source-preserving chunking helpers for semantic retrieval."""

from __future__ import annotations

import re
import uuid

from app.core.models import DocumentType, TextChunk


def _split_text(text: str, max_chars: int = 900, overlap_chars: int = 140) -> list[str]:
    """Split text at whitespace boundaries while retaining modest context overlap."""
    normalized = re.sub(r"[ \t]+", " ", text).strip()
    if len(normalized) <= max_chars:
        return [normalized] if normalized else []

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        if end < len(normalized):
            boundary = max(normalized.rfind(". ", start, end), normalized.rfind(" ", start, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(normalized):
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def chunks_from_page(
    *, document_id: str, filename: str, page_number: int, text: str
) -> list[TextChunk]:
    """Create chunks that can always be attributed to exactly one PDF page."""
    return [
        TextChunk(
            chunk_id=str(uuid.uuid4()),
            document_id=document_id,
            filename=filename,
            document_type=DocumentType.PDF,
            text=piece,
            page_number=page_number,
        )
        for piece in _split_text(text)
    ]


def chunks_from_lines(
    *, document_id: str, filename: str, lines: list[str], lines_per_chunk: int = 24
) -> list[TextChunk]:
    """Create TXT chunks with exact original line ranges and a nearby heading."""
    chunks: list[TextChunk] = []
    current_section: str | None = None
    for start_index in range(0, len(lines), lines_per_chunk):
        segment = lines[start_index : start_index + lines_per_chunk]
        for line in segment:
            stripped = line.strip()
            if stripped and (stripped.endswith(":") or (len(stripped) < 80 and stripped.isupper())):
                current_section = stripped.rstrip(":")
        text = "\n".join(segment).strip()
        if not text:
            continue
        chunks.append(
            TextChunk(
                chunk_id=str(uuid.uuid4()),
                document_id=document_id,
                filename=filename,
                document_type=DocumentType.TXT,
                text=text,
                line_start=start_index + 1,
                line_end=min(start_index + len(segment), len(lines)),
                section=current_section,
            )
        )
    return chunks
