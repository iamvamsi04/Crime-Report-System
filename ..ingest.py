from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import pymupdf

from app.config import Settings
from app.errors import DuplicateDocumentError, FileValidationError
from app.gemini import GeminiService
from app.models import DocumentOut
from app.storage import Storage, utcnow

log = logging.getLogger(__name__)

SUPPORTED = {".pdf": "pdf", ".txt": "txt", ".csv": "csv", ".xlsx": "excel", ".xls": "excel"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
YEAR_COL_HINTS = {"year", "yr", "fiscal_year", "fy", "period"}
ENTITY_COL_HINTS = {"department", "entity", "name", "team", "division", "company", "org", "organization"}


def ingest_bytes(
    *,
    filename: str,
    data: bytes,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> DocumentOut:
    log.info("document_upload filename=%s bytes=%s", _display_name(filename), len(data))
    _validate_upload(filename, data, settings)
    file_type = SUPPORTED[Path(filename).suffix.lower()]
    digest = hashlib.sha256(data).hexdigest()
    existing = store.get_document_by_hash(digest)
    if existing:
        raise DuplicateDocumentError("This file was already uploaded.", existing["id"])

    document_id = str(uuid.uuid4())
    stored_name = f"{document_id}{Path(filename).suffix.lower()}"
    dest = settings.upload_dir / stored_name
    dest.write_bytes(data)
    now = utcnow()
    display = _display_name(filename)
    store.insert_document(
        {
            "id": document_id,
            "filename": display,
            "stored_name": stored_name,
            "file_type": file_type,
            "file_hash": digest,
            "status": "processing",
            "chunk_count": 0,
            "page_count": None,
            "csv_profile": None,
            "error_message": None,
            "created_at": now,
            "updated_at": now,
        }
    )
    try:
        extracted = extract_document(dest, display, file_type)
        chunks = build_chunks(extracted, settings)
        if not chunks:
            raise FileValidationError("The document did not contain usable text.")
        log.info("chunk_creation document_id=%s count=%s", document_id, len(chunks))
        embeddings = gemini.embed_texts([c["text"] for c in chunks])
        ids = [f"{document_id}:{c['chunk_id']}" for c in chunks]
        metadatas = [_chunk_metadata(document_id, display, file_type, c) for c in chunks]
        store.upsert_chunks(ids, [c["text"] for c in chunks], embeddings, metadatas)
        csv_profile = extracted.get("csv_profile")
        store.update_document(
            document_id,
            status="ready",
            chunk_count=len(chunks),
            page_count=extracted.get("page_count"),
            csv_profile=json.dumps(csv_profile) if csv_profile else None,
            error_message=None,
        )
        log.info("document_ready document_id=%s chunks=%s", document_id, len(chunks))
    except Exception:
        store.purge_ingest(document_id)
        if dest.exists():
            dest.unlink()
        raise

    doc = store.get_document(document_id)
    return to_document_out(store, doc)


def _validate_upload(filename: str, data: bytes, settings: Settings) -> None:
    if not data:
        raise FileValidationError("The uploaded file is empty.")
    if len(data) > settings.max_upload_bytes:
        raise FileValidationError("The uploaded file exceeds the size limit.")
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise FileValidationError("Unsupported file type. Upload a PDF, TXT, CSV, or Excel (.xlsx, .xls) file.")


def _display_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    if not name or name in {".", ".."}:
        raise FileValidationError("The filename is invalid.")
    cleaned = SAFE_NAME.sub("_", name).strip("._")
    if not cleaned:
        raise FileValidationError("The filename is invalid.")
    return cleaned[:180]


def extract_document(path: Path, filename: str, file_type: str) -> dict[str, Any]:
    log.info("document_parsing filename=%s type=%s", filename, file_type)
    if file_type == "pdf":
        return _extract_pdf(path, filename)
    if file_type == "txt":
        return _extract_txt(path, filename)
    return _extract_table(path, filename, file_type)


def _extract_pdf(path: Path, filename: str) -> dict[str, Any]:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise FileValidationError("The PDF file is corrupt or unreadable.") from exc
    pages: list[dict[str, Any]] = []
    try:
        if doc.is_encrypted:
            raise FileValidationError("Encrypted PDF files are not supported.")
        for page in doc:
            text = page.get_text("text") or ""
            section = _first_heading(text)
            pages.append({"page_number": page.number + 1, "text": text, "section": section})
    finally:
        doc.close()
    combined = "\n".join(p["text"] for p in pages).strip()
    if not combined:
        raise FileValidationError("The PDF does not contain extractable text.")
    return {"filename": filename, "file_type": "pdf", "pages": pages, "page_count": len(pages)}


def _extract_txt(path: Path, filename: str) -> dict[str, Any]:
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8", "utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
        log.warning("txt_encoding_replaced filename=%s", filename)
    if not text.strip():
        raise FileValidationError("The text file is empty.")
    lines = text.splitlines()
    return {
        "filename": filename,
        "file_type": "txt",
        "text": text,
        "lines": lines,
        "page_count": None,
    }


def _extract_table(path: Path, filename: str, file_type: str) -> dict[str, Any]:
    label = "Excel" if file_type == "excel" else "CSV"
    try:
        frame = pd.read_excel(path, sheet_name=0) if file_type == "excel" else pd.read_csv(path)
    except Exception as exc:
        raise FileValidationError(f"The {label} file is malformed and could not be parsed.") from exc
    if frame.empty or len(frame.columns) == 0:
        raise FileValidationError(f"The {label} file does not contain usable rows or columns.")
    frame.columns = [str(c).strip() for c in frame.columns]
    if any(c == "" or c.startswith("Unnamed") for c in frame.columns) and list(frame.columns) == [""]:
        raise FileValidationError(f"The {label} file is malformed and could not be parsed.")
    profile = _csv_profile(frame)
    log.info("csv_profile filename=%s rows=%s cols=%s", filename, profile["row_count"], profile["columns"])
    return {
        "filename": filename,
        "file_type": file_type,
        "frame": frame,
        "csv_profile": profile,
        "page_count": None,
    }


def _csv_profile(frame: pd.DataFrame) -> dict[str, Any]:
    dtypes = {c: str(frame[c].dtype) for c in frame.columns}
    numeric = [c for c in frame.columns if pd.api.types.is_numeric_dtype(frame[c])]
    year_cols = [c for c in frame.columns if c.lower().replace(" ", "_") in YEAR_COL_HINTS]
    entity_cols = [c for c in frame.columns if c.lower().replace(" ", "_") in ENTITY_COL_HINTS]
    if not year_cols:
        for col in frame.columns:
            sample = pd.to_numeric(frame[col], errors="coerce").dropna()
            if not sample.empty and sample.between(1900, 2100).mean() > 0.8:
                year_cols.append(col)
    return {
        "columns": list(frame.columns),
        "dtypes": dtypes,
        "row_count": int(len(frame)),
        "null_counts": {c: int(frame[c].isna().sum()) for c in frame.columns},
        "numeric_columns": numeric,
        "year_columns": year_cols,
        "entity_columns": entity_cols,
        "categorical_columns": [c for c in frame.columns if c not in numeric],
    }


def build_chunks(extracted: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    file_type = extracted["file_type"]
    if file_type == "pdf":
        return _chunk_pdf(extracted, settings)
    if file_type == "txt":
        return _chunk_txt(extracted, settings)
    return _chunk_csv(extracted)


def _chunk_pdf(extracted: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for page in extracted["pages"]:
        page_no = page["page_number"]
        section = page.get("section") or ""
        windows = _window(page["text"], settings.chunk_size, settings.chunk_overlap)
        if not windows and page["text"].strip():
            windows = [page["text"].strip()]
        for text in windows:
            chunk_id = f"c{len(chunks) + 1:04d}"
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "text": text,
                    "page_number": page_no,
                    "section": section or _first_heading(text),
                    "source_reference": f"{extracted['filename']}, p. {page_no}",
                    "start_line": 0,
                    "end_line": 0,
                    "row_start": 0,
                    "row_end": 0,
                    "columns": "",
                    "entities": "",
                    "year": _detect_year(text),
                }
            )
    return chunks


def _chunk_txt(extracted: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    lines: list[str] = extracted["lines"]
    chunks: list[dict[str, Any]] = []
    buf: list[str] = []
    start = 1
    size = 0
    section = ""

    def flush(end_line: int) -> None:
        nonlocal buf, start, size, section
        text = "\n".join(buf).strip()
        if not text:
            buf, size = [], 0
            return
        chunk_id = f"c{len(chunks) + 1:04d}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "text": text,
                "page_number": 0,
                "section": section or _first_heading(text),
                "source_reference": f"{extracted['filename']}, lines {start}-{end_line}",
                "start_line": start,
                "end_line": end_line,
                "row_start": 0,
                "row_end": 0,
                "columns": "",
                "entities": "",
                "year": _detect_year(text),
            }
        )
        overlap_chars = settings.chunk_overlap
        kept: list[str] = []
        total = 0
        for line in reversed(buf):
            if total + len(line) + 1 > overlap_chars:
                break
            kept.append(line)
            total += len(line) + 1
        kept.reverse()
        buf = kept
        size = sum(len(x) + 1 for x in buf)
        start = max(end_line - len(kept) + 1, 1) if kept else end_line + 1

    for idx, line in enumerate(lines, start=1):
        heading = _heading_line(line)
        if heading:
            section = heading
        buf.append(line)
        size += len(line) + 1
        if size >= settings.chunk_size:
            flush(idx)
    if buf:
        flush(len(lines))
    return chunks


def _chunk_csv(extracted: dict[str, Any]) -> list[dict[str, Any]]:
    frame: pd.DataFrame = extracted["frame"]
    profile = extracted["csv_profile"]
    filename = extracted["filename"]
    chunks: list[dict[str, Any]] = []

    schema = (
        f"{extracted['file_type'].upper()} file {filename} with {profile['row_count']} rows. "
        f"Columns: {', '.join(profile['columns'])}. "
        f"Types: {json.dumps(profile['dtypes'])}. "
        f"Numeric columns: {', '.join(profile['numeric_columns']) or 'none'}."
    )
    chunks.append(_csv_chunk(len(chunks), schema, filename, 1, len(frame), ",".join(profile["columns"]), "", 0))

    for col in profile["numeric_columns"]:
        series = pd.to_numeric(frame[col], errors="coerce")
        text = (
            f"{filename} column {col}: min={series.min()}, max={series.max()}, "
            f"mean={series.mean()}, nulls={int(series.isna().sum())}"
        )
        chunks.append(_csv_chunk(len(chunks), text, filename, 1, len(frame), col, "", 0))

    entity_col = profile["entity_columns"][0] if profile["entity_columns"] else None
    year_col = profile["year_columns"][0] if profile["year_columns"] else None
    value_cols = profile["numeric_columns"][:4]
    if entity_col and year_col and value_cols:
        grouped = frame.groupby([entity_col, year_col], dropna=False)
        for (entity, year), group in grouped:
            parts = [f"{entity_col}={entity}", f"{year_col}={year}"]
            for col in value_cols:
                if col in (entity_col, year_col):
                    continue
                numeric = pd.to_numeric(group[col], errors="coerce")
                if numeric.notna().any():
                    parts.append(f"{col}={numeric.sum() if len(numeric.dropna()) > 1 else numeric.dropna().iloc[0]}")
            text = f"{filename} record: " + "; ".join(str(p) for p in parts)
            year_int = _coerce_year(year)
            row_start = int(group.index.min()) + 2
            row_end = int(group.index.max()) + 2
            chunks.append(
                _csv_chunk(
                    len(chunks),
                    text,
                    filename,
                    row_start,
                    row_end,
                    ",".join([entity_col, year_col, *value_cols]),
                    str(entity),
                    year_int,
                )
            )
    elif len(frame) <= 40:
        for idx, row in frame.iterrows():
            text = f"{filename} row {int(idx) + 2}: " + ", ".join(f"{c}={row[c]}" for c in frame.columns)
            chunks.append(
                _csv_chunk(
                    len(chunks),
                    text,
                    filename,
                    int(idx) + 2,
                    int(idx) + 2,
                    ",".join(frame.columns),
                    "",
                    _detect_year(text),
                )
            )
    return chunks


def _csv_chunk(
    index: int,
    text: str,
    filename: str,
    row_start: int,
    row_end: int,
    columns: str,
    entities: str,
    year: int,
) -> dict[str, Any]:
    chunk_id = f"c{index + 1:04d}"
    return {
        "chunk_id": chunk_id,
        "text": text,
        "page_number": 0,
        "section": "",
        "source_reference": f"{filename}, rows {row_start}-{row_end}",
        "start_line": 0,
        "end_line": 0,
        "row_start": row_start,
        "row_end": row_end,
        "columns": columns,
        "entities": entities,
        "year": year,
    }


def _chunk_metadata(document_id: str, filename: str, file_type: str, chunk: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": chunk["chunk_id"],
        "filename": filename,
        "document_type": file_type,
        "source_reference": chunk["source_reference"],
        "page_number": int(chunk["page_number"] or 0),
        "section": chunk.get("section") or "",
        "start_line": int(chunk.get("start_line") or 0),
        "end_line": int(chunk.get("end_line") or 0),
        "row_start": int(chunk.get("row_start") or 0),
        "row_end": int(chunk.get("row_end") or 0),
        "columns": chunk.get("columns") or "",
        "entities": chunk.get("entities") or "",
        "year": int(chunk.get("year") or 0),
    }


def _window(text: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start, end), text.rfind("\n", start, end))
            if cut > start + size // 3:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            parts.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return parts


def _first_heading(text: str) -> str:
    for line in text.splitlines():
        heading = _heading_line(line)
        if heading:
            return heading
    return ""


def _heading_line(line: str) -> str:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return ""
    if stripped.endswith(":") or re.match(r"^[A-Z][A-Za-z0-9 /&-]{2,}$", stripped):
        return stripped.rstrip(":")
    return ""


def _detect_year(text: str) -> int:
    match = re.search(r"\b(20\d{2}|19\d{2})\b", text)
    return int(match.group(1)) if match else 0


def _coerce_year(value: Any) -> int:
    try:
        year = int(float(value))
        return year if 1900 <= year <= 2100 else 0
    except (TypeError, ValueError):
        return _detect_year(str(value))


def to_document_out(store: Storage, doc: dict[str, Any]) -> DocumentOut:
    return DocumentOut(
        document_id=doc["id"],
        filename=doc["filename"],
        file_type=doc["file_type"],
        file_hash=doc["file_hash"],
        status=doc["status"],
        chunk_count=int(doc["chunk_count"] or 0),
        page_count=doc["page_count"],
        csv_profile=store.parse_csv_profile(doc.get("csv_profile")),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
    )

