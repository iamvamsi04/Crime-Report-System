from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import Settings
from app.errors import (
    DuplicateDocumentError,
    FileValidationError,
    GeminiError,
)
from app.gemini import GeminiService
from app.storage import Storage

log = logging.getLogger(__name__)


SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".txt",
    ".csv",
    ".xlsx",
    ".xls",
}


def ingest_document(
    file_path: Path,
    *,
    filename: str,
    store: Storage,
    gemini: GeminiService,
    settings: Settings,
) -> dict[str, Any]:
    """
    Ingest a document into the existing application storage.

    CSV/Excel files are profiled with Pandas so their schema remains
    available through documents.csv_profile.

    The original uploaded file is preserved in data/uploads and is later
    queried directly by DuckDB during analysis.
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileValidationError(
            f"File does not exist: {file_path}"
        )

    extension = file_path.suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise FileValidationError(
            f"Unsupported file type: {extension}"
        )

    file_size = file_path.stat().st_size

    if file_size > settings.max_upload_bytes:
        raise FileValidationError(
            "Uploaded file exceeds the maximum allowed size."
        )

    file_hash = _file_hash(file_path)

    existing = store.get_document_by_hash(file_hash)

    if existing is not None:
        raise DuplicateDocumentError(
            "A document with the same content already exists."
        )

    document = store.create_document(
        filename=filename,
        file_type=extension.lstrip("."),
        file_hash=file_hash,
        status="processing",
    )

    document_id = document["id"]

    try:
        if extension in {".csv", ".xlsx", ".xls"}:
            profile = _build_csv_profile(
                file_path,
                extension,
            )

            store.update_document(
                document_id,
                csv_profile=json.dumps(profile),
            )

        else:
            profile = None

        # The existing application ingestion/chunking pipeline remains
        # responsible for extracting document content and creating
        # embeddings. This function only prepares the structured profile
        # used by the analysis layer.
        return {
            "document_id": document_id,
            "filename": filename,
            "file_type": extension.lstrip("."),
            "csv_profile": profile,
        }

    except Exception as exc:
        log.exception(
            "Document ingestion failed for %s",
            filename,
        )

        store.update_document(
            document_id,
            status="failed",
            error_message=str(exc),
        )

        raise


def _build_csv_profile(
    file_path: Path,
    extension: str,
) -> dict[str, Any]:
    """
    Build the schema/profile stored in documents.csv_profile.

    Pandas is intentionally retained here. DuckDB is used later for
    executing analytical SQL against the original file.
    """
    dataframe = _read_table(
        file_path,
        extension,
    )

    columns = [
        str(column)
        for column in dataframe.columns
    ]

    dtypes = {
        str(column): str(dtype)
        for column, dtype in dataframe.dtypes.items()
    }

    numeric_columns = [
        str(column)
        for column in dataframe.select_dtypes(
            include="number"
        ).columns
    ]

    year_columns = _detect_year_columns(
        dataframe,
    )

    entity_columns = _detect_entity_columns(
        dataframe,
    )

    categorical_columns = [
        str(column)
        for column in dataframe.select_dtypes(
            include=["object", "category", "string"]
        ).columns
    ]

    null_counts = {
        str(column): int(value)
        for column, value in dataframe.isna().sum().items()
    }

    return {
        "columns": columns,
        "dtypes": dtypes,
        "row_count": int(len(dataframe)),
        "null_counts": null_counts,
        "numeric_columns": numeric_columns,
        "year_columns": year_columns,
        "entity_columns": entity_columns,
        "categorical_columns": categorical_columns,
    }


def _read_table(
    file_path: Path,
    extension: str,
) -> pd.DataFrame:
    try:
        if extension == ".csv":
            return pd.read_csv(file_path)

        if extension in {".xlsx", ".xls"}:
            return pd.read_excel(file_path)

    except Exception as exc:
        raise FileValidationError(
            f"Unable to read tabular file: {exc}"
        ) from exc

    raise FileValidationError(
        f"Unsupported tabular extension: {extension}"
    )


def _detect_year_columns(
    dataframe: pd.DataFrame,
) -> list[str]:
    year_columns: list[str] = []

    for column in dataframe.columns:
        name = str(column).strip().lower()

        if (
            name == "year"
            or name.endswith("_year")
            or "year" in name
        ):
            year_columns.append(str(column))
            continue

        series = dataframe[column]

        if pd.api.types.is_numeric_dtype(series):
            non_null = series.dropna()

            if not non_null.empty:
                try:
                    numeric_values = pd.to_numeric(
                        non_null,
                        errors="coerce",
                    ).dropna()

                    if not numeric_values.empty:
                        if numeric_values.between(
                            1900,
                            2100,
                        ).mean() >= 0.95:
                            year_columns.append(
                                str(column)
                            )
                except Exception:
                    continue

    return _deduplicate(year_columns)


def _detect_entity_columns(
    dataframe: pd.DataFrame,
) -> list[str]:
    entity_columns: list[str] = []

    for column in dataframe.columns:
        name = str(column).strip().lower()

        if any(
            token in name
            for token in (
                "department",
                "category",
                "segment",
                "product",
                "region",
                "country",
                "state",
                "city",
                "employee",
                "customer",
                "company",
                "division",
                "team",
                "entity",
            )
        ):
            entity_columns.append(str(column))
            continue

        series = dataframe[column]

        if (
            pd.api.types.is_object_dtype(series)
            or pd.api.types.is_string_dtype(series)
            or pd.api.types.is_categorical_dtype(series)
        ):
            non_null = series.dropna()

            if non_null.empty:
                continue

            unique_count = non_null.nunique()

            if 1 < unique_count <= min(
                100,
                max(2, len(dataframe) // 2),
            ):
                entity_columns.append(str(column))

    return _deduplicate(entity_columns)


def _deduplicate(
    values: list[str],
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []

    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)

    return result


def _file_hash(
    file_path: Path,
) -> str:
    digest = hashlib.sha256()

    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()
