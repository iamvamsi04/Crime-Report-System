"""CSV validation and schema profiling for deterministic local analysis."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.core.exceptions import FileValidationError
from app.core.models import DocumentRecord, DocumentStatus, DocumentType


def read_csv_safely(path: Path) -> pd.DataFrame:
    """Read a conventional UTF-8 CSV and convert parser failures into clear errors."""
    try:
        dataframe = pd.read_csv(path)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise FileValidationError(f"The CSV file '{path.name}' is empty, malformed, or not UTF-8 encoded.") from exc
    except OSError as exc:
        raise FileValidationError(f"Unable to read CSV file '{path.name}'.") from exc
    if dataframe.empty:
        raise FileValidationError(f"The CSV file '{path.name}' has no data rows.")
    if not list(dataframe.columns) or any(not str(column).strip() for column in dataframe.columns):
        raise FileValidationError(f"The CSV file '{path.name}' has invalid column headers.")
    return dataframe


def ingest_csv(path: Path, document_id: str) -> tuple[DocumentRecord, pd.DataFrame]:
    """Profile a CSV without sending its rows to any external service."""
    dataframe = read_csv_safely(path)
    return (
        DocumentRecord(
            document_id=document_id,
            filename=path.name,
            path=str(path),
            document_type=DocumentType.CSV,
            status=DocumentStatus.READY,
            row_count=len(dataframe),
            columns=[str(column) for column in dataframe.columns],
        ),
        dataframe,
    )
