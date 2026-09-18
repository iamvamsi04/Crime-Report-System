from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import fitz
import numpy as np
import pandas as pd

from app.config import Settings
from app.dataset import prepare_dataframe, read_dataframe
from app.errors import (
    DuplicateDocumentError,
    FileValidationError,
)
from app.gemini import GeminiService
from app.storage import Storage, utcnow

log = logging.getLogger(__name__)


SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".csv",
    ".xlsx",
    ".xls",
}

STRUCTURED_EXTENSIONS = {
    ".csv",
    ".xlsx",
    ".xls",
}

TEXT_EXTENSIONS = {
    ".pdf",
    ".txt",
}

TEXT_ENCODINGS = (
    "utf-8",
    "utf-8-sig",
    "cp1252",
    "latin1",
)

PROFILE_SAMPLE_VALUES = 12
PROFILE_TOP_VALUES = 10
MAX_PROFILE_STRING = 250

# Embedding chunks are for discovery/semantic context only.
# They are NOT the source of truth for structured analysis.
MAX_STRUCTURED_DISCOVERY_COLUMNS = 80
MAX_STRUCTURED_DISCOVERY_VALUES = 8


def ingest_bytes(
    *,
    filename: str,
    data: bytes,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> dict[str, Any]:
    """
    Validate, persist, extract, chunk, embed, and register an uploaded file.

    For CSV/Excel:
        - the complete original file is preserved on disk;
        - rich schema metadata is stored in csv_profile;
        - lightweight schema/discovery chunks are embedded;
        - later analysis reloads the complete table via dataset.py.

    The embeddings are never treated as a substitute for the full table.
    """

    safe_filename = _validate_upload(
        filename=filename,
        data=data,
        settings=settings,
    )

    extension = Path(
        safe_filename
    ).suffix.lower()

    file_hash = hashlib.sha256(
        data
    ).hexdigest()

    existing = store.get_document_by_hash(
        file_hash
    )

    if existing is not None:
        raise DuplicateDocumentError(
            "This document has already been uploaded."
        )

    document_id = str(
        uuid.uuid4()
    )

    stored_name = (
        f"{document_id}{extension}"
    )

    path = store.upload_path(
        stored_name
    )

    document = {
        "id": document_id,
        "filename": safe_filename,
        "stored_name": stored_name,
        "file_type": extension.lstrip("."),
        "file_hash": file_hash,
        "status": "processing",
        "chunk_count": 0,
        "page_count": None,
        "csv_profile": None,
        "error_message": None,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }

    try:
        _write_upload(
            path=path,
            data=data,
        )

        store.create_document(
            document
        )

        extracted = _extract_document(
            path=path,
            extension=extension,
            filename=safe_filename,
        )

        chunks = build_chunks(
            document_id=document_id,
            filename=safe_filename,
            extension=extension,
            extracted=extracted,
            settings=settings,
        )

        if not chunks:
            raise FileValidationError(
                "The uploaded document did not contain readable content."
            )

        _embed_chunks(
            chunks=chunks,
            store=store,
            gemini=gemini,
            settings=settings,
        )

        page_count = extracted.get(
            "page_count"
        )

        profile = extracted.get(
            "profile"
        )

        store.update_document(
            document_id,
            {
                "status": "ready",
                "chunk_count": len(
                    chunks
                ),
                "page_count": page_count,
                "csv_profile": (
                    json.dumps(
                        profile,
                        ensure_ascii=False,
                        default=str,
                    )
                    if profile is not None
                    else None
                ),
                "error_message": None,
                "updated_at": utcnow(),
            },
        )

        ready = store.get_document(
            document_id
        )

        return ready

    except Exception as exc:
        log.exception(
            "document_ingestion_failed "
            "document_id=%s filename=%s",
            document_id,
            safe_filename,
        )

        try:
            existing_document = store.get_document(
                document_id
            )

            if existing_document is not None:
                store.update_document(
                    document_id,
                    {
                        "status": "error",
                        "error_message": _safe_error(
                            exc
                        ),
                        "updated_at": utcnow(),
                    },
                )
        except Exception:
            log.exception(
                "failed_to_record_ingestion_error "
                "document_id=%s",
                document_id,
            )

        # If the DB record was never created, remove the orphaned upload.
        try:
            if (
                path.exists()
                and store.get_document(
                    document_id
                ) is None
            ):
                path.unlink(
                    missing_ok=True
                )
        except Exception:
            log.exception(
                "failed_to_cleanup_orphan_upload "
                "document_id=%s",
                document_id,
            )

        raise


def build_chunks(
    *,
    document_id: str,
    filename: str,
    extension: str,
    extracted: dict[str, Any],
    settings: Settings,
) -> list[dict[str, Any]]:
    if extension == ".pdf":
        return _chunk_pdf(
            document_id=document_id,
            filename=filename,
            pages=extracted["pages"],
            settings=settings,
        )

    if extension == ".txt":
        return _chunk_text(
            document_id=document_id,
            filename=filename,
            text=extracted["text"],
            settings=settings,
        )

    if extension in STRUCTURED_EXTENSIONS:
        return _chunk_structured(
            document_id=document_id,
            filename=filename,
            dataframe=extracted["dataframe"],
            profile=extracted["profile"],
        )

    raise FileValidationError(
        f"Unsupported file type: {extension}"
    )


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _extract_document(
    *,
    path: Path,
    extension: str,
    filename: str,
) -> dict[str, Any]:
    if extension == ".pdf":
        return _extract_pdf(
            path
        )

    if extension == ".txt":
        return {
            "text": _extract_text(
                path
            )
        }

    if extension in STRUCTURED_EXTENSIONS:
        return _extract_table(
            path=path,
            extension=extension,
            filename=filename,
        )

    raise FileValidationError(
        f"Unsupported file type: {extension}"
    )


def _extract_pdf(
    path: Path,
) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []

    try:
        document = fitz.open(
            path
        )
    except Exception as exc:
        raise FileValidationError(
            "The PDF could not be opened."
        ) from exc

    try:
        for page_index in range(
            len(document)
        ):
            page = document[
                page_index
            ]

            text = page.get_text(
                "text"
            )

            text = _normalize_text(
                text
            )

            pages.append(
                {
                    "page_number": (
                        page_index + 1
                    ),
                    "text": text,
                }
            )
    finally:
        document.close()

    readable_pages = [
        page
        for page in pages
        if page["text"].strip()
    ]

    if not readable_pages:
        raise FileValidationError(
            "No readable text was found in the PDF."
        )

    return {
        "pages": pages,
        "page_count": len(
            pages
        ),
    }


def _extract_text(
    path: Path,
) -> str:
    raw = path.read_bytes()

    for encoding in TEXT_ENCODINGS:
        try:
            text = raw.decode(
                encoding
            )

            text = _normalize_text(
                text
            )

            if text.strip():
                return text

        except UnicodeDecodeError:
            continue

    raise FileValidationError(
        "The text file could not be decoded."
    )


def _extract_table(
    *,
    path: Path,
    extension: str,
    filename: str,
) -> dict[str, Any]:
    try:
        dataframe = read_dataframe(
            path=path,
            file_type=extension,
        )

        dataframe = prepare_dataframe(
            dataframe
        )

    except Exception as exc:
        raise FileValidationError(
            f"The structured file '{filename}' could not be read."
        ) from exc

    if dataframe.shape[1] == 0:
        raise FileValidationError(
            "The structured file contains no columns."
        )

    profile = _table_profile(
        dataframe=dataframe,
        filename=filename,
    )

    return {
        "dataframe": dataframe,
        "profile": profile,
    }


# ---------------------------------------------------------------------------
# Structured profiling
# ---------------------------------------------------------------------------


def _table_profile(
    *,
    dataframe: pd.DataFrame,
    filename: str,
) -> dict[str, Any]:
    """
    Rich metadata for planning and SQL generation.

    No business-domain assumptions are made here. We profile what actually
    exists instead of guessing concepts such as department/revenue/year.
    """

    column_profiles = [
        _column_profile(
            dataframe[column],
            name=str(column),
        )
        for column in dataframe.columns
    ]

    numeric_columns = [
        profile["name"]
        for profile in column_profiles
        if profile["semantic_type"]
        in {
            "integer",
            "number",
        }
    ]

    date_columns = [
        profile["name"]
        for profile in column_profiles
        if profile["semantic_type"]
        in {
            "date",
            "datetime",
        }
    ]

    categorical_columns = [
        profile["name"]
        for profile in column_profiles
        if profile["semantic_type"]
        in {
            "text",
            "boolean",
            "category",
        }
        and profile["unique_count"]
        <= max(
            100,
            int(
                len(dataframe)
                * 0.25
            ),
        )
    ]

    sample_values = {
        profile["name"]: profile[
            "sample_values"
        ]
        for profile in column_profiles
        if profile["sample_values"]
    }

    return {
        "filename": filename,
        "row_count": int(
            len(dataframe)
        ),
        "column_count": int(
            len(dataframe.columns)
        ),
        "columns": [
            str(column)
            for column in dataframe.columns
        ],
        "dtypes": {
            str(column): str(
                dataframe[column].dtype
            )
            for column in dataframe.columns
        },
        "null_counts": {
            str(column): int(
                dataframe[column]
                .isna()
                .sum()
            )
            for column in dataframe.columns
        },
        "numeric_columns": (
            numeric_columns
        ),
        "date_columns": (
            date_columns
        ),
        "categorical_columns": (
            categorical_columns
        ),
        "sample_values": (
            sample_values
        ),
        "column_profiles": (
            column_profiles
        ),
    }


def _column_profile(
    series: pd.Series,
    *,
    name: str,
) -> dict[str, Any]:
    non_null = series.dropna()

    null_count = int(
        series.isna().sum()
    )

    try:
        unique_count = int(
            non_null.nunique(
                dropna=True
            )
        )
    except TypeError:
        unique_count = len(
            {
                str(value)
                for value in non_null
            }
        )

    semantic_type = (
        _semantic_type(
            series
        )
    )

    profile: dict[str, Any] = {
        "name": name,
        "dtype": str(
            series.dtype
        ),
        "semantic_type": (
            semantic_type
        ),
        "nullable": (
            null_count > 0
        ),
        "null_count": (
            null_count
        ),
        "unique_count": (
            unique_count
        ),
        "sample_values": (
            _representative_values(
                non_null
            )
        ),
    }

    minimum, maximum = (
        _min_max(
            non_null,
            semantic_type,
        )
    )

    if minimum is not None:
        profile["minimum"] = minimum

    if maximum is not None:
        profile["maximum"] = maximum

    top_values = _top_values(
        non_null
    )

    if top_values:
        profile["top_values"] = (
            top_values
        )

    return profile


def _semantic_type(
    series: pd.Series,
) -> str:
    dtype = series.dtype

    if pd.api.types.is_bool_dtype(
        dtype
    ):
        return "boolean"

    if pd.api.types.is_integer_dtype(
        dtype
    ):
        return "integer"

    if pd.api.types.is_float_dtype(
        dtype
    ):
        return "number"

    if pd.api.types.is_datetime64_any_dtype(
        dtype
    ):
        return "datetime"

    if isinstance(
        dtype,
        pd.CategoricalDtype,
    ):
        return "category"

    if _looks_date_like(
        series
    ):
        return "date"

    return "text"


def _looks_date_like(
    series: pd.Series,
) -> bool:
    """
    Conservative detection used only as metadata.

    We do not mutate the source column into datetime here.
    """

    non_null = (
        series.dropna()
        .astype(str)
        .str.strip()
    )

    if non_null.empty:
        return False

    sample = non_null.head(
        50
    )

    # Avoid interpreting ordinary integer-like identifiers as dates.
    if sample.str.fullmatch(
        r"[-+]?\d+(?:\.\d+)?"
    ).all():
        return False

    try:
        parsed = pd.to_datetime(
            sample,
            errors="coerce",
        )
    except Exception:
        return False

    ratio = float(
        parsed.notna().mean()
    )

    return ratio >= 0.8


def _representative_values(
    series: pd.Series,
) -> list[Any]:
    if series.empty:
        return []

    values: list[Any] = []
    seen: set[str] = set()

    total = len(series)

    if total <= PROFILE_SAMPLE_VALUES:
        indexes = range(
            total
        )
    else:
        indexes = np.linspace(
            0,
            total - 1,
            num=PROFILE_SAMPLE_VALUES,
            dtype=int,
        )

    reset = series.reset_index(
        drop=True
    )

    for index in indexes:
        value = _json_safe(
            reset.iloc[
                int(index)
            ]
        )

        key = repr(
            value
        )

        if key in seen:
            continue

        seen.add(key)

        if isinstance(
            value,
            str,
        ):
            value = value[
                :MAX_PROFILE_STRING
            ]

        values.append(
            value
        )

    return values


def _top_values(
    series: pd.Series,
) -> list[dict[str, Any]]:
    if series.empty:
        return []

    # Frequency summaries are useful for categorical discovery, but can be
    # expensive/noisy for effectively unique columns.
    try:
        unique_count = int(
            series.nunique(
                dropna=True
            )
        )
    except TypeError:
        return []

    if unique_count > max(
        1000,
        len(series) // 2,
    ):
        return []

    try:
        counts = (
            series.value_counts(
                dropna=True
            )
            .head(
                PROFILE_TOP_VALUES
            )
        )
    except Exception:
        return []

    result: list[
        dict[str, Any]
    ] = []

    for value, count in counts.items():
        safe_value = _json_safe(
            value
        )

        if isinstance(
            safe_value,
            str,
        ):
            safe_value = safe_value[
                :MAX_PROFILE_STRING
            ]

        result.append(
            {
                "value": safe_value,
                "count": int(
                    count
                ),
            }
        )

    return result


def _min_max(
    series: pd.Series,
    semantic_type: str,
) -> tuple[Any | None, Any | None]:
    if series.empty:
        return None, None

    if semantic_type not in {
        "integer",
        "number",
        "date",
        "datetime",
    }:
        return None, None

    try:
        if semantic_type == "date":
            parsed = pd.to_datetime(
                series,
                errors="coerce",
            ).dropna()

            if parsed.empty:
                return None, None

            return (
                _json_safe(
                    parsed.min()
                ),
                _json_safe(
                    parsed.max()
                ),
            )

        return (
            _json_safe(
                series.min()
            ),
            _json_safe(
                series.max()
            ),
        )

    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


def _chunk_pdf(
    *,
    document_id: str,
    filename: str,
    pages: list[dict[str, Any]],
    settings: Settings,
) -> list[dict[str, Any]]:
    chunks: list[
        dict[str, Any]
    ] = []

    for page in pages:
        text = str(
            page.get("text") or ""
        ).strip()

        if not text:
            continue

        page_number = int(
            page["page_number"]
        )

        windows = _text_windows(
            text=text,
            chunk_size=int(
                settings.chunk_size
            ),
            overlap=int(
                settings.chunk_overlap
            ),
        )

        for index, window in enumerate(
            windows
        ):
            chunks.append(
                {
                    "id": str(
                        uuid.uuid4()
                    ),
                    "document_id": (
                        document_id
                    ),
                    "filename": filename,
                    "document_type": "pdf",
                    "text": window,
                    "embedding_text": (
                        f"Document: {filename}\n"
                        f"Page: {page_number}\n\n"
                        f"{window}"
                    ),
                    "source_reference": (
                        f"{filename}, page "
                        f"{page_number}"
                    ),
                    "page_number": (
                        page_number
                    ),
                    "section": None,
                    "start_line": None,
                    "end_line": None,
                    "row_start": None,
                    "row_end": None,
                }
            )

    return chunks


def _chunk_text(
    *,
    document_id: str,
    filename: str,
    text: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    lines = text.splitlines()

    if not lines:
        return []

    chunk_size = max(
        100,
        int(
            settings.chunk_size
        ),
    )

    overlap = max(
        0,
        int(
            settings.chunk_overlap
        ),
    )

    chunks: list[
        dict[str, Any]
    ] = []

    buffer: list[str] = []
    buffer_start = 1
    current_length = 0

    for line_number, line in enumerate(
        lines,
        start=1,
    ):
        addition = (
            len(line) + 1
        )

        if (
            buffer
            and current_length
            + addition
            > chunk_size
        ):
            chunk_text = "\n".join(
                buffer
            ).strip()

            if chunk_text:
                end_line = (
                    line_number - 1
                )

                chunks.append(
                    {
                        "id": str(
                            uuid.uuid4()
                        ),
                        "document_id": (
                            document_id
                        ),
                        "filename": filename,
                        "document_type": (
                            "txt"
                        ),
                        "text": chunk_text,
                        "embedding_text": (
                            f"Document: {filename}\n"
                            f"Lines: {buffer_start}-"
                            f"{end_line}\n\n"
                            f"{chunk_text}"
                        ),
                        "source_reference": (
                            f"{filename}, lines "
                            f"{buffer_start}-"
                            f"{end_line}"
                        ),
                        "page_number": None,
                        "section": None,
                        "start_line": (
                            buffer_start
                        ),
                        "end_line": (
                            end_line
                        ),
                        "row_start": None,
                        "row_end": None,
                    }
                )

            buffer, buffer_start = (
                _overlap_lines(
                    buffer=buffer,
                    end_line=line_number - 1,
                    overlap_chars=overlap,
                )
            )

            current_length = sum(
                len(value) + 1
                for value in buffer
            )

        if not buffer:
            buffer_start = (
                line_number
            )

        buffer.append(
            line
        )

        current_length += (
            addition
        )

    if buffer:
        chunk_text = "\n".join(
            buffer
        ).strip()

        if chunk_text:
            chunks.append(
                {
                    "id": str(
                        uuid.uuid4()
                    ),
                    "document_id": (
                        document_id
                    ),
                    "filename": filename,
                    "document_type": "txt",
                    "text": chunk_text,
                    "embedding_text": (
                        f"Document: {filename}\n"
                        f"Lines: {buffer_start}-"
                        f"{len(lines)}\n\n"
                        f"{chunk_text}"
                    ),
                    "source_reference": (
                        f"{filename}, lines "
                        f"{buffer_start}-"
                        f"{len(lines)}"
                    ),
                    "page_number": None,
                    "section": None,
                    "start_line": (
                        buffer_start
                    ),
                    "end_line": len(
                        lines
                    ),
                    "row_start": None,
                    "row_end": None,
                }
            )

    return chunks


def _text_windows(
    *,
    text: str,
    chunk_size: int,
    overlap: int,
) -> list[str]:
    chunk_size = max(
        100,
        chunk_size,
    )

    overlap = max(
        0,
        min(
            overlap,
            chunk_size - 1,
        ),
    )

    text = text.strip()

    if not text:
        return []

    if len(text) <= chunk_size:
        return [
            text
        ]

    windows: list[str] = []

    start = 0
    length = len(
        text
    )

    while start < length:
        end = min(
            start + chunk_size,
            length,
        )

        if end < length:
            boundary = _best_boundary(
                text=text,
                start=start,
                end=end,
            )

            if boundary > start:
                end = boundary

        window = text[
            start:end
        ].strip()

        if window:
            windows.append(
                window
            )

        if end >= length:
            break

        next_start = max(
            end - overlap,
            start + 1,
        )

        start = next_start

    return windows


def _best_boundary(
    *,
    text: str,
    start: int,
    end: int,
) -> int:
    search_start = max(
        start,
        end - 300,
    )

    segment = text[
        search_start:end
    ]

    candidates = [
        segment.rfind("\n\n"),
        segment.rfind("\n"),
        segment.rfind(". "),
        segment.rfind(" "),
    ]

    best = max(
        candidates
    )

    if best < 0:
        return end

    boundary = (
        search_start
        + best
        + 1
    )

    if boundary <= start:
        return end

    return boundary


def _overlap_lines(
    *,
    buffer: list[str],
    end_line: int,
    overlap_chars: int,
) -> tuple[list[str], int]:
    if (
        overlap_chars <= 0
        or not buffer
    ):
        return [], (
            end_line + 1
        )

    selected: list[str] = []
    total = 0

    for line in reversed(
        buffer
    ):
        selected.append(
            line
        )

        total += (
            len(line) + 1
        )

        if total >= overlap_chars:
            break

    selected.reverse()

    start_line = (
        end_line
        - len(selected)
        + 1
    )

    return (
        selected,
        max(
            1,
            start_line,
        ),
    )


# ---------------------------------------------------------------------------
# Structured discovery chunks
# ---------------------------------------------------------------------------


def _chunk_structured(
    *,
    document_id: str,
    filename: str,
    dataframe: pd.DataFrame,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Produce lightweight semantic-discovery chunks.

    Important:
        These chunks are NOT used to compute structured answers.

    They help document discovery/planning and provide human-readable schema
    metadata in the vector store. The full dataframe remains on disk and is
    queried later by DuckDB.
    """

    chunks: list[
        dict[str, Any]
    ] = []

    summary = _structured_summary(
        filename=filename,
        profile=profile,
    )

    chunks.append(
        _structured_chunk(
            document_id=document_id,
            filename=filename,
            text=summary,
            source_reference=(
                f"{filename}, table schema"
            ),
            section="table_schema",
        )
    )

    column_profiles = profile.get(
        "column_profiles",
        [],
    )

    # Group column descriptions so very wide tables do not create one vector
    # per column.
    batch_size = 12

    for start in range(
        0,
        min(
            len(column_profiles),
            MAX_STRUCTURED_DISCOVERY_COLUMNS,
        ),
        batch_size,
    ):
        batch = column_profiles[
            start:start + batch_size
        ]

        text = _column_batch_text(
            filename=filename,
            columns=batch,
        )

        chunks.append(
            _structured_chunk(
                document_id=document_id,
                filename=filename,
                text=text,
                source_reference=(
                    f"{filename}, columns "
                    f"{start + 1}-"
                    f"{start + len(batch)}"
                ),
                section="column_metadata",
            )
        )

    # A tiny sample is useful for semantic discovery, but we intentionally do
    # not embed every row.
    sample_text = _small_row_sample(
        filename=filename,
        dataframe=dataframe,
    )

    if sample_text:
        chunks.append(
            _structured_chunk(
                document_id=document_id,
                filename=filename,
                text=sample_text,
                source_reference=(
                    f"{filename}, representative rows"
                ),
                section="representative_rows",
                row_start=1,
                row_end=min(
                    len(dataframe),
                    5,
                ),
            )
        )

    return chunks


def _structured_summary(
    *,
    filename: str,
    profile: dict[str, Any],
) -> str:
    columns = profile.get(
        "columns",
        [],
    )

    visible_columns = columns[
        :MAX_STRUCTURED_DISCOVERY_COLUMNS
    ]

    lines = [
        f"Structured dataset: {filename}",
        (
            f"Rows: {profile.get('row_count', 0)}"
        ),
        (
            f"Columns: {profile.get('column_count', len(columns))}"
        ),
        "Column names:",
        ", ".join(
            str(column)
            for column in visible_columns
        ),
    ]

    if len(columns) > len(
        visible_columns
    ):
        lines.append(
            (
                f"{len(columns) - len(visible_columns)} "
                "additional columns are present."
            )
        )

    numeric = profile.get(
        "numeric_columns",
        [],
    )

    dates = profile.get(
        "date_columns",
        [],
    )

    categorical = profile.get(
        "categorical_columns",
        [],
    )

    if numeric:
        lines.append(
            "Numeric columns: "
            + ", ".join(
                str(value)
                for value in numeric[
                    :40
                ]
            )
        )

    if dates:
        lines.append(
            "Date-like columns: "
            + ", ".join(
                str(value)
                for value in dates[
                    :40
                ]
            )
        )

    if categorical:
        lines.append(
            "Categorical/text columns: "
            + ", ".join(
                str(value)
                for value in categorical[
                    :40
                ]
            )
        )

    lines.append(
        (
            "The complete table is preserved for direct structured "
            "analysis; this text is only a discovery summary."
        )
    )

    return "\n".join(
        lines
    )


def _column_batch_text(
    *,
    filename: str,
    columns: list[dict[str, Any]],
) -> str:
    lines = [
        f"Structured dataset column metadata: {filename}"
    ]

    for column in columns:
        name = str(
            column.get("name") or ""
        )

        semantic_type = str(
            column.get(
                "semantic_type"
            )
            or column.get(
                "dtype"
            )
            or "unknown"
        )

        unique_count = column.get(
            "unique_count"
        )

        null_count = column.get(
            "null_count"
        )

        samples = list(
            column.get(
                "sample_values"
            )
            or []
        )[
            :MAX_STRUCTURED_DISCOVERY_VALUES
        ]

        description = [
            f"Column: {name}",
            f"type={semantic_type}",
        ]

        if unique_count is not None:
            description.append(
                f"unique={unique_count}"
            )

        if null_count is not None:
            description.append(
                f"nulls={null_count}"
            )

        if samples:
            description.append(
                "examples="
                + json.dumps(
                    samples,
                    ensure_ascii=False,
                    default=str,
                )
            )

        if "minimum" in column:
            description.append(
                "min="
                + str(
                    column["minimum"]
                )
            )

        if "maximum" in column:
            description.append(
                "max="
                + str(
                    column["maximum"]
                )
            )

        lines.append(
            "; ".join(
                description
            )
        )

    return "\n".join(
        lines
    )


def _small_row_sample(
    *,
    filename: str,
    dataframe: pd.DataFrame,
) -> str:
    if dataframe.empty:
        return ""

    sample = dataframe.head(
        5
    )

    # Keep discovery text bounded for very wide datasets.
    sample = sample.iloc[
        :,
        :min(
            len(sample.columns),
            25,
        ),
    ]

    records = []

    for raw in sample.to_dict(
        orient="records"
    ):
        records.append(
            {
                str(key): _json_safe(
                    value
                )
                for key, value in raw.items()
            }
        )

    return "\n".join(
        [
            (
                f"Representative rows from structured dataset: "
                f"{filename}"
            ),
            json.dumps(
                records,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            (
                "These are representative rows only, not the complete "
                "dataset."
            ),
        ]
    )


def _structured_chunk(
    *,
    document_id: str,
    filename: str,
    text: str,
    source_reference: str,
    section: str,
    row_start: int | None = None,
    row_end: int | None = None,
) -> dict[str, Any]:
    return {
        "id": str(
            uuid.uuid4()
        ),
        "document_id": document_id,
        "filename": filename,
        "document_type": (
            _structured_document_type(
                filename
            )
        ),
        "text": text,
        "embedding_text": text,
        "source_reference": (
            source_reference
        ),
        "page_number": None,
        "section": section,
        "start_line": None,
        "end_line": None,
        "row_start": row_start,
        "row_end": row_end,
    }


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def _embed_chunks(
    *,
    chunks: list[dict[str, Any]],
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> None:
    batch_size = max(
        1,
        int(
            getattr(
                settings,
                "embed_batch_size",
                32,
            )
        ),
    )

    for start in range(
        0,
        len(chunks),
        batch_size,
    ):
        batch = chunks[
            start:start + batch_size
        ]

        texts = [
            str(
                chunk.get(
                    "embedding_text"
                )
                or chunk["text"]
            )
            for chunk in batch
        ]

        embeddings = gemini.embed_texts(
            texts
        )

        if len(embeddings) != len(
            batch
        ):
            raise FileValidationError(
                "Embedding service returned an unexpected number of vectors."
            )

        store.upsert_chunks(
            chunks=batch,
            embeddings=embeddings,
        )


# ---------------------------------------------------------------------------
# Validation / persistence
# ---------------------------------------------------------------------------


def _validate_upload(
    *,
    filename: str,
    data: bytes,
    settings: Settings,
) -> str:
    if not filename:
        raise FileValidationError(
            "A filename is required."
        )

    safe_filename = Path(
        filename
    ).name.strip()

    if not safe_filename:
        raise FileValidationError(
            "A valid filename is required."
        )

    extension = Path(
        safe_filename
    ).suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise FileValidationError(
            "Unsupported file type. Upload PDF, TXT, CSV, XLSX, or XLS."
        )

    if not data:
        raise FileValidationError(
            "The uploaded file is empty."
        )

    max_bytes = int(
        settings.max_upload_bytes
    )

    if len(data) > max_bytes:
        raise FileValidationError(
            (
                "The uploaded file exceeds the maximum allowed size "
                f"of {max_bytes} bytes."
            )
        )

    return safe_filename


def _write_upload(
    *,
    path: Path,
    data: bytes,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    try:
        temporary.write_bytes(
            data
        )

        shutil.move(
            str(temporary),
            str(path),
        )

    finally:
        temporary.unlink(
            missing_ok=True
        )


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------


def _normalize_text(
    text: str,
) -> str:
    text = text.replace(
        "\x00",
        ""
    )

    text = text.replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    )

    # Preserve line boundaries for TXT citations, but remove excessive
    # horizontal whitespace.
    text = re.sub(
        r"[ \t]+",
        " ",
        text,
    )

    text = re.sub(
        r"\n{4,}",
        "\n\n\n",
        text,
    )

    return text.strip()


def _structured_document_type(
    filename: str,
) -> str:
    suffix = Path(
        filename
    ).suffix.lower().lstrip(".")

    if suffix in {
        "xlsx",
        "xls",
    }:
        return suffix

    return "csv"


def _json_safe(
    value: Any,
) -> Any:
    if value is None:
        return None

    try:
        missing = pd.isna(
            value
        )

        if isinstance(
            missing,
            (bool, np.bool_),
        ) and missing:
            return None
    except (
        TypeError,
        ValueError,
    ):
        pass

    if isinstance(
        value,
        np.integer,
    ):
        return int(
            value
        )

    if isinstance(
        value,
        np.floating,
    ):
        number = float(
            value
        )

        if (
            math.isnan(number)
            or math.isinf(number)
        ):
            return None

        return number

    if isinstance(
        value,
        np.bool_,
    ):
        return bool(
            value
        )

    if isinstance(
        value,
        (
            pd.Timestamp,
            datetime,
            date,
        ),
    ):
        return value.isoformat()

    if isinstance(
        value,
        pd.Timedelta,
    ):
        return str(
            value
        )

    if isinstance(
        value,
        float,
    ):
        if (
            math.isnan(value)
            or math.isinf(value)
        ):
            return None

    if isinstance(
        value,
        str,
    ):
        return value[
            :MAX_PROFILE_STRING
        ]

    if isinstance(
        value,
        (
            int,
            float,
            bool,
        ),
    ):
        return value

    return str(
        value
    )[
        :MAX_PROFILE_STRING
    ]


def _safe_error(
    exc: Exception,
) -> str:
    text = str(
        exc
    ).strip()

    if not text:
        text = (
            exc.__class__.__name__
        )

    if len(text) > 2_000:
        text = (
            text[:2_000]
            + "…"
        )

    return text
