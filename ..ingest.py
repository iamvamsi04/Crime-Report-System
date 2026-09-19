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


SUPPORTED = {
    ".pdf": "pdf",
    ".txt": "txt",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".xls": "xls",
}

SAFE_NAME = re.compile(
    r"[^A-Za-z0-9._-]+"
)

YEAR_COL_HINTS = {
    "year",
    "yr",
    "fiscal_year",
    "fy",
    "period",
}

ENTITY_COL_HINTS = {
    "department",
    "entity",
    "name",
    "team",
    "division",
    "company",
    "org",
    "organization",
}


def ingest_bytes(
    *,
    filename: str,
    data: bytes,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> DocumentOut:
    """
    Validate and ingest an uploaded document.

    Processing strategy:

        PDF/TXT
            -> extract text
            -> chunk
            -> Gemini embeddings
            -> Chroma

        CSV/Excel
            -> validate
            -> create structural profile
            -> store original file
            -> DuckDB handles future questions

    CSV/Excel files are intentionally NOT embedded into Chroma because
    structured questions should be answered by querying the original
    structured data with DuckDB.
    """

    log.info(
        "document_upload filename=%s bytes=%s",
        _display_name(filename),
        len(data),
    )

    _validate_upload(
        filename,
        data,
        settings,
    )

    suffix = Path(filename).suffix.lower()

    file_type = SUPPORTED[suffix]

    digest = hashlib.sha256(
        data
    ).hexdigest()

    existing = store.get_document_by_hash(
        digest
    )

    if existing:
        raise DuplicateDocumentError(
            "This file was already uploaded.",
            existing["id"],
        )

    document_id = str(
        uuid.uuid4()
    )

    stored_name = (
        f"{document_id}{suffix}"
    )

    dest = (
        settings.upload_dir
        / stored_name
    )

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
        extracted = extract_document(
            dest,
            display,
            file_type,
        )

        csv_profile = extracted.get(
            "csv_profile"
        )

        # -------------------------------------------------------------
        # Structured documents
        #
        # CSV and Excel are deliberately NOT embedded.
        # DuckDB will query the original file later.
        # -------------------------------------------------------------
        if file_type in {
            "csv",
            "xlsx",
            "xls",
        }:
            store.update_document(
                document_id,
                status="ready",
                chunk_count=0,
                page_count=None,
                csv_profile=(
                    json.dumps(
                        csv_profile
                    )
                    if csv_profile
                    else None
                ),
                error_message=None,
            )

            log.info(
                "structured_document_ready "
                "document_id=%s filename=%s rows=%s",
                document_id,
                display,
                (
                    csv_profile.get(
                        "row_count"
                    )
                    if csv_profile
                    else 0
                ),
            )

        # -------------------------------------------------------------
        # Unstructured documents
        #
        # PDF/TXT use the RAG pipeline.
        # -------------------------------------------------------------
        else:
            chunks = build_chunks(
                extracted,
                settings,
            )

            if not chunks:
                raise FileValidationError(
                    "The document did not contain usable text."
                )

            log.info(
                "chunk_creation document_id=%s count=%s",
                document_id,
                len(chunks),
            )

            embeddings = gemini.embed_texts(
                [
                    chunk["text"]
                    for chunk in chunks
                ]
            )

            if len(embeddings) != len(chunks):
                raise FileValidationError(
                    "The embedding service returned an unexpected "
                    "number of embeddings."
                )

            ids = [
                (
                    f"{document_id}:"
                    f"{chunk['chunk_id']}"
                )
                for chunk in chunks
            ]

            metadatas = [
                _chunk_metadata(
                    document_id,
                    display,
                    file_type,
                    chunk,
                )
                for chunk in chunks
            ]

            store.upsert_chunks(
                ids,
                [
                    chunk["text"]
                    for chunk in chunks
                ],
                embeddings,
                metadatas,
            )

            store.update_document(
                document_id,
                status="ready",
                chunk_count=len(chunks),
                page_count=extracted.get(
                    "page_count"
                ),
                csv_profile=None,
                error_message=None,
            )

            log.info(
                "document_ready "
                "document_id=%s chunks=%s",
                document_id,
                len(chunks),
            )

    except Exception:
        log.exception(
            "document_ingestion_failed "
            "document_id=%s filename=%s",
            document_id,
            display,
        )

        store.purge_ingest(
            document_id
        )

        if dest.exists():
            dest.unlink()

        raise

    doc = store.get_document(
        document_id
    )

    if doc is None:
        raise FileValidationError(
            "The document was ingested but could not "
            "be loaded from storage."
        )

    return to_document_out(
        store,
        doc,
    )


def _validate_upload(
    filename: str,
    data: bytes,
    settings: Settings,
) -> None:
    if not data:
        raise FileValidationError(
            "The uploaded file is empty."
        )

    if len(data) > settings.max_upload_bytes:
        raise FileValidationError(
            "The uploaded file exceeds the size limit."
        )

    suffix = Path(
        filename
    ).suffix.lower()

    if suffix not in SUPPORTED:
        raise FileValidationError(
            "Unsupported file type. Upload a PDF, TXT, CSV, "
            "or Excel (.xlsx, .xls) file."
        )


def _display_name(
    filename: str,
) -> str:
    name = Path(
        filename.replace(
            "\\",
            "/",
        )
    ).name

    if not name or name in {
        ".",
        "..",
    }:
        raise FileValidationError(
            "The filename is invalid."
        )

    cleaned = SAFE_NAME.sub(
        "_",
        name,
    ).strip("._")

    if not cleaned:
        raise FileValidationError(
            "The filename is invalid."
        )

    return cleaned[:180]


def extract_document(
    path: Path,
    filename: str,
    file_type: str,
) -> dict[str, Any]:
    log.info(
        "document_parsing filename=%s type=%s",
        filename,
        file_type,
    )

    if file_type == "pdf":
        return _extract_pdf(
            path,
            filename,
        )

    if file_type == "txt":
        return _extract_txt(
            path,
            filename,
        )

    return _extract_table(
        path,
        filename,
        file_type,
    )


def _extract_pdf(
    path: Path,
    filename: str,
) -> dict[str, Any]:
    try:
        doc = pymupdf.open(
            path
        )
    except Exception as exc:
        raise FileValidationError(
            "The PDF file is corrupt or unreadable."
        ) from exc

    pages: list[dict[str, Any]] = []

    try:
        if doc.is_encrypted:
            raise FileValidationError(
                "Encrypted PDF files are not supported."
            )

        for page in doc:
            text = page.get_text(
                "text"
            ) or ""

            if not text.strip():
                continue

            section = _first_heading(
                text
            )

            pages.append(
                {
                    "page_number": (
                        page.number + 1
                    ),
                    "text": text,
                    "section": section,
                }
            )

    finally:
        doc.close()

    combined = "\n".join(
        page["text"]
        for page in pages
    ).strip()

    if not combined:
        raise FileValidationError(
            "The PDF does not contain extractable text."
        )

    return {
        "filename": filename,
        "file_type": "pdf",
        "pages": pages,
        "page_count": len(pages),
    }


def _extract_txt(
    path: Path,
    filename: str,
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except Exception as exc:
        raise FileValidationError(
            "The text file could not be read."
        ) from exc

    text = None

    for encoding in (
        "utf-8",
        "utf-8-sig",
        "cp1252",
    ):
        try:
            text = raw.decode(
                encoding
            )
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        text = raw.decode(
            "utf-8",
            errors="replace",
        )

        log.warning(
            "txt_encoding_replaced filename=%s",
            filename,
        )

    if not text.strip():
        raise FileValidationError(
            "The text file is empty."
        )

    lines = text.splitlines()

    return {
        "filename": filename,
        "file_type": "txt",
        "text": text,
        "lines": lines,
        "page_count": None,
    }


def _extract_table(
    path: Path,
    filename: str,
    file_type: str,
) -> dict[str, Any]:
    label = (
        "Excel"
        if file_type in {
            "xlsx",
            "xls",
        }
        else "CSV"
    )

    try:
        if file_type in {
            "xlsx",
            "xls",
        }:
            frame = pd.read_excel(
                path,
                sheet_name=0,
            )
        else:
            frame = pd.read_csv(
                path
            )

    except Exception as exc:
        raise FileValidationError(
            f"The {label} file is malformed and "
            "could not be parsed."
        ) from exc

    if (
        frame.empty
        or len(frame.columns) == 0
    ):
        raise FileValidationError(
            f"The {label} file does not contain "
            "usable rows or columns."
        )

    cleaned_columns: list[str] = []

    for column in frame.columns:
        value = str(
            column
        ).strip()

        if not value:
            raise FileValidationError(
                f"The {label} file contains an empty "
                "column name."
            )

        cleaned_columns.append(
            value
        )

    if len(
        set(cleaned_columns)
    ) != len(cleaned_columns):
        raise FileValidationError(
            f"The {label} file contains duplicate column names."
        )

    frame.columns = cleaned_columns

    if any(
        column.startswith("Unnamed:")
        for column in frame.columns
    ):
        log.warning(
            "table_contains_unnamed_columns filename=%s",
            filename,
        )

    profile = _csv_profile(
        frame
    )

    log.info(
        "structured_profile filename=%s "
        "rows=%s cols=%s",
        filename,
        profile["row_count"],
        profile["columns"],
    )

    return {
        "filename": filename,
        "file_type": file_type,
        "frame": frame,
        "csv_profile": profile,
        "page_count": None,
    }


def _csv_profile(
    frame: pd.DataFrame,
) -> dict[str, Any]:
    dtypes = {
        str(column): str(
            frame[column].dtype
        )
        for column in frame.columns
    }

    numeric = [
        str(column)
        for column in frame.columns
        if pd.api.types.is_numeric_dtype(
            frame[column]
        )
    ]

    year_cols = [
        str(column)
        for column in frame.columns
        if str(column)
        .lower()
        .strip()
        .replace(
            " ",
            "_",
        )
        in YEAR_COL_HINTS
    ]

    entity_cols = [
        str(column)
        for column in frame.columns
        if str(column)
        .lower()
        .strip()
        .replace(
            " ",
            "_",
        )
        in ENTITY_COL_HINTS
    ]

    if not year_cols:
        for column in frame.columns:
            series = pd.to_numeric(
                frame[column],
                errors="coerce",
            ).dropna()

            if series.empty:
                continue

            if (
                series.between(
                    1900,
                    2100,
                ).mean()
                > 0.8
            ):
                year_cols.append(
                    str(column)
                )

    categorical = [
        str(column)
        for column in frame.columns
        if str(column) not in numeric
    ]

    return {
        "columns": [
            str(column)
            for column in frame.columns
        ],
        "dtypes": dtypes,
        "row_count": int(
            len(frame)
        ),
        "null_counts": {
            str(column): int(
                frame[column].isna().sum()
            )
            for column in frame.columns
        },
        "numeric_columns": numeric,
        "year_columns": year_cols,
        "entity_columns": entity_cols,
        "categorical_columns": categorical,
    }


def build_chunks(
    extracted: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    """
    Build RAG chunks only for unstructured documents.

    CSV and Excel intentionally return no chunks because their questions
    are handled by DuckDB against the original file.
    """

    file_type = extracted[
        "file_type"
    ]

    if file_type == "pdf":
        return _chunk_pdf(
            extracted,
            settings,
        )

    if file_type == "txt":
        return _chunk_txt(
            extracted,
            settings,
        )

    return []


def _chunk_pdf(
    extracted: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []

    for page in extracted[
        "pages"
    ]:
        page_no = int(
            page["page_number"]
        )

        section = (
            page.get("section")
            or ""
        )

        windows = _window(
            page["text"],
            settings.chunk_size,
            settings.chunk_overlap,
        )

        if (
            not windows
            and page["text"].strip()
        ):
            windows = [
                page["text"].strip()
            ]

        for text in windows:
            chunk_id = (
                f"c{len(chunks) + 1:04d}"
            )

            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "text": text,
                    "page_number": page_no,
                    "section": (
                        section
                        or _first_heading(
                            text
                        )
                    ),
                    "source_reference": (
                        f"{extracted['filename']}, "
                        f"p. {page_no}"
                    ),
                    "start_line": 0,
                    "end_line": 0,
                    "row_start": 0,
                    "row_end": 0,
                    "columns": "",
                    "entities": "",
                    "year": _detect_year(
                        text
                    ),
                }
            )

    return chunks


def _chunk_txt(
    extracted: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    lines: list[str] = (
        extracted["lines"]
    )

    chunks: list[dict[str, Any]] = []

    buf: list[str] = []
    start = 1
    size = 0
    section = ""

    def flush(
        end_line: int,
    ) -> None:
        nonlocal buf
        nonlocal start
        nonlocal size
        nonlocal section

        text = "\n".join(
            buf
        ).strip()

        if not text:
            buf = []
            size = 0
            return

        chunk_id = (
            f"c{len(chunks) + 1:04d}"
        )

        chunks.append(
            {
                "chunk_id": chunk_id,
                "text": text,
                "page_number": 0,
                "section": (
                    section
                    or _first_heading(
                        text
                    )
                ),
                "source_reference": (
                    f"{extracted['filename']}, "
                    f"lines {start}-{end_line}"
                ),
                "start_line": start,
                "end_line": end_line,
                "row_start": 0,
                "row_end": 0,
                "columns": "",
                "entities": "",
                "year": _detect_year(
                    text
                ),
            }
        )

        overlap_chars = max(
            0,
            settings.chunk_overlap,
        )

        kept: list[str] = []
        total = 0

        for line in reversed(buf):
            line_size = (
                len(line) + 1
            )

            if (
                total + line_size
                > overlap_chars
            ):
                break

            kept.append(
                line
            )
            total += line_size

        kept.reverse()

        buf = kept

        size = sum(
            len(line) + 1
            for line in buf
        )

        if kept:
            start = max(
                end_line
                - len(kept)
                + 1,
                1,
            )
        else:
            start = (
                end_line + 1
            )

    for idx, line in enumerate(
        lines,
        start=1,
    ):
        heading = _heading_line(
            line
        )

        if heading:
            section = heading

        buf.append(
            line
        )

        size += (
            len(line) + 1
        )

        if size >= settings.chunk_size:
            flush(idx)

    if buf:
        flush(len(lines))

    return chunks


def _chunk_metadata(
    document_id: str,
    filename: str,
    file_type: str,
    chunk: dict[str, Any],
) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": chunk[
            "chunk_id"
        ],
        "filename": filename,
        "document_type": file_type,
        "source_reference": chunk[
            "source_reference"
        ],
        "page_number": int(
            chunk.get(
                "page_number",
                0,
            )
            or 0
        ),
        "section": (
            chunk.get(
                "section"
            )
            or ""
        ),
        "start_line": int(
            chunk.get(
                "start_line",
                0,
            )
            or 0
        ),
        "end_line": int(
            chunk.get(
                "end_line",
                0,
            )
            or 0
        ),
        "row_start": int(
            chunk.get(
                "row_start",
                0,
            )
            or 0
        ),
        "row_end": int(
            chunk.get(
                "row_end",
                0,
            )
            or 0
        ),
        "columns": (
            chunk.get(
                "columns"
            )
            or ""
        ),
        "entities": (
            chunk.get(
                "entities"
            )
            or ""
        ),
        "year": int(
            chunk.get(
                "year",
                0,
            )
            or 0
        ),
    }


def _window(
    text: str,
    size: int,
    overlap: int,
) -> list[str]:
    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text,
    ).strip()

    if not text:
        return []

    size = max(
        1,
        size,
    )

    overlap = max(
        0,
        min(
            overlap,
            size - 1,
        ),
    )

    if len(text) <= size:
        return [text]

    parts: list[str] = []
    start = 0

    while start < len(text):
        end = min(
            start + size,
            len(text),
        )

        if end < len(text):
            cut = max(
                text.rfind(
                    "\n\n",
                    start,
                    end,
                ),
                text.rfind(
                    ". ",
                    start,
                    end,
                ),
                text.rfind(
                    "\n",
                    start,
                    end,
                ),
            )

            if cut > (
                start + size // 3
            ):
                end = cut + 1

        piece = text[
            start:end
        ].strip()

        if piece:
            parts.append(
                piece
            )

        if end >= len(text):
            break

        start = max(
            end - overlap,
            start + 1,
        )

    return parts


def _first_heading(
    text: str,
) -> str:
    for line in text.splitlines():
        heading = _heading_line(
            line
        )

        if heading:
            return heading

    return ""


def _heading_line(
    line: str,
) -> str:
    stripped = line.strip()

    if (
        not stripped
        or len(stripped) > 80
    ):
        return ""

    if (
        stripped.endswith(":")
        or re.match(
            r"^[A-Z][A-Za-z0-9 /&-]{2,}$",
            stripped,
        )
    ):
        return stripped.rstrip(
            ":"
        )

    return ""


def _detect_year(
    text: str,
) -> int:
    match = re.search(
        r"\b(20\d{2}|19\d{2})\b",
        text,
    )

    if not match:
        return 0

    return int(
        match.group(1)
    )


def to_document_out(
    store: Storage,
    doc: dict[str, Any],
) -> DocumentOut:
    return DocumentOut(
        document_id=doc["id"],
        filename=doc["filename"],
        file_type=doc["file_type"],
        file_hash=doc["file_hash"],
        status=doc["status"],
        chunk_count=int(
            doc["chunk_count"] or 0
        ),
        page_count=doc[
            "page_count"
        ],
        csv_profile=store.parse_csv_profile(
            doc.get(
                "csv_profile"
            )
        ),
        created_at=doc[
            "created_at"
        ],
        updated_at=doc[
            "updated_at"
        ],
    )
