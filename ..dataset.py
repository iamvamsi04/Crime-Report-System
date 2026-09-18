from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.errors import AnalysisError
from app.models import ColumnSchema, DatasetInfo, DatasetSchema, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)


SUPPORTED_TABLE_TYPES = {"csv", "xlsx", "xls"}

MAX_SCHEMA_SAMPLE_VALUES = 8
MAX_STRING_SAMPLE_LENGTH = 120


@dataclass
class LoadedDataset:
    """
    Runtime representation of one structured document.

    The physical file path is never exposed to Gemini. Gemini only sees
    safe table names such as dataset_1, dataset_2, etc.
    """

    document_id: str
    filename: str
    file_type: str
    table_name: str
    dataframe: pd.DataFrame
    schema: DatasetSchema

    @property
    def info(self) -> DatasetInfo:
        return DatasetInfo(
            document_id=self.document_id,
            filename=self.filename,
            table_name=self.table_name,
            row_count=len(self.dataframe),
            column_count=len(self.dataframe.columns),
        )


def load_datasets(
    plan: QueryPlan,
    store: Storage,
) -> list[LoadedDataset]:
    """
    Load structured datasets selected by the query plan.

    Selection priority:
        1. Explicit document IDs
        2. Filename hints
        3. All ready CSV/Excel documents

    Only CSV/XLS/XLSX documents are considered.
    """

    documents = store.list_documents(ready_only=True)

    structured_documents = [
        doc
        for doc in documents
        if _normalized_file_type(doc) in SUPPORTED_TABLE_TYPES
    ]

    if not structured_documents:
        raise AnalysisError(
            "No ready CSV or Excel documents are available for analysis."
        )

    selected = _select_documents(
        documents=structured_documents,
        document_ids=plan.document_ids,
        filename_hints=plan.document_hints,
    )

    if not selected:
        raise AnalysisError(
            "No matching CSV or Excel document was found for this question."
        )

    loaded: list[LoadedDataset] = []

    for index, document in enumerate(selected, start=1):
        table_name = f"dataset_{index}"

        loaded.append(
            load_dataset(
                document=document,
                table_name=table_name,
                store=store,
            )
        )

    return loaded


def load_dataset(
    document: dict[str, Any],
    table_name: str,
    store: Storage,
) -> LoadedDataset:
    """
    Load one CSV/Excel document and build its runtime schema.
    """

    document_id = str(document["id"])
    filename = str(document["filename"])
    file_type = _normalized_file_type(document)

    if file_type not in SUPPORTED_TABLE_TYPES:
        raise AnalysisError(
            f"{filename} is not a supported structured-data document."
        )

    path = store.document_file(document_id)

    try:
        frame = read_dataframe(path, file_type)
    except AnalysisError:
        raise
    except Exception as exc:
        log.exception(
            "dataset_load_failed document_id=%s filename=%s",
            document_id,
            filename,
        )
        raise AnalysisError(
            f"Could not load structured document '{filename}'."
        ) from exc

    if frame.empty and len(frame.columns) == 0:
        raise AnalysisError(
            f"Structured document '{filename}' does not contain a usable table."
        )

    frame = prepare_dataframe(frame)

    schema = build_dataset_schema(
        dataframe=frame,
        document_id=document_id,
        filename=filename,
        table_name=table_name,
    )

    log.info(
        "dataset_loaded document_id=%s filename=%s rows=%s columns=%s table=%s",
        document_id,
        filename,
        len(frame),
        len(frame.columns),
        table_name,
    )

    return LoadedDataset(
        document_id=document_id,
        filename=filename,
        file_type=file_type,
        table_name=table_name,
        dataframe=frame,
        schema=schema,
    )


def read_dataframe(
    path: Path,
    file_type: str | None = None,
) -> pd.DataFrame:
    """
    Centralized dataframe reader.

    All backend modules should use this function instead of independently
    calling pd.read_csv() or pd.read_excel().
    """

    kind = (file_type or path.suffix.lstrip(".")).lower()

    try:
        if kind == "csv":
            return _read_csv(path)

        if kind in {"xlsx", "xls"}:
            return pd.read_excel(path, sheet_name=0)

    except Exception as exc:
        log.exception(
            "dataframe_read_failed path=%s type=%s",
            path.name,
            kind,
        )
        raise AnalysisError(
            f"Could not read structured file '{path.name}'."
        ) from exc

    raise AnalysisError(
        f"Unsupported structured file type: {kind or 'unknown'}."
    )


def _read_csv(path: Path) -> pd.DataFrame:
    """
    Read CSV with a few safe encoding fallbacks.

    Pandas still performs delimiter/type inference.
    """

    encodings = ("utf-8", "utf-8-sig", "cp1252", "latin1")
    last_error: Exception | None = None

    for encoding in encodings:
        try:
            return pd.read_csv(
                path,
                encoding=encoding,
                low_memory=False,
            )
        except UnicodeDecodeError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error

    return pd.read_csv(path, low_memory=False)


def prepare_dataframe(frame: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare a dataframe for reliable DuckDB analysis without changing
    the user's actual semantic column names.

    Important:
    - We do NOT rename columns to guessed business concepts.
    - We do NOT guess which column is entity/year/revenue/etc.
    - We only make duplicate/blank column names uniquely addressable.
    """

    frame = frame.copy()

    frame.columns = _unique_column_names(
        [str(column).strip() for column in frame.columns]
    )

    # Convert Pandas extension values that can occasionally cause awkward
    # DuckDB registration behavior into stable representations.
    for column in frame.columns:
        series = frame[column]

        if pd.api.types.is_categorical_dtype(series.dtype):
            frame[column] = series.astype("string")

    return frame


def build_dataset_schema(
    dataframe: pd.DataFrame,
    document_id: str,
    filename: str,
    table_name: str,
) -> DatasetSchema:
    """
    Build rich schema metadata for Gemini.

    This is intentionally domain-independent. We expose actual column
    information and representative values rather than hard-coded concepts.
    """

    columns: list[ColumnSchema] = []

    for column_name in dataframe.columns:
        series = dataframe[column_name]

        columns.append(
            build_column_schema(
                name=str(column_name),
                series=series,
            )
        )

    return DatasetSchema(
        document_id=document_id,
        filename=filename,
        table_name=table_name,
        row_count=len(dataframe),
        column_count=len(dataframe.columns),
        columns=columns,
    )


def build_column_schema(
    name: str,
    series: pd.Series,
) -> ColumnSchema:
    """
    Create useful metadata for one column.

    Gemini receives:
    - real column name
    - dtype
    - null count
    - approximate uniqueness
    - representative values
    - min/max when meaningful

    This gives it enough information to reason about unfamiliar datasets.
    """

    null_count = int(series.isna().sum())
    nullable = null_count > 0

    non_null = series.dropna()

    unique_count: int | None = None

    try:
        unique_count = int(non_null.nunique(dropna=True))
    except Exception:
        pass

    samples = _sample_values(non_null)

    minimum: Any | None = None
    maximum: Any | None = None

    if not non_null.empty:
        try:
            if (
                pd.api.types.is_numeric_dtype(non_null.dtype)
                or pd.api.types.is_datetime64_any_dtype(non_null.dtype)
            ):
                minimum = _json_safe(non_null.min())
                maximum = _json_safe(non_null.max())
        except Exception:
            minimum = None
            maximum = None

    return ColumnSchema(
        name=name,
        dtype=_friendly_dtype(series),
        nullable=nullable,
        null_count=null_count,
        unique_count=unique_count,
        sample_values=samples,
        minimum=minimum,
        maximum=maximum,
    )


def schemas_for_prompt(
    datasets: list[LoadedDataset],
) -> list[dict[str, Any]]:
    """
    Produce JSON-serializable schema information for Gemini.

    Physical paths and internal storage details are deliberately excluded.
    """

    return [
        dataset.schema.model_dump(mode="json")
        for dataset in datasets
    ]


def dataset_catalog(
    datasets: list[LoadedDataset],
) -> list[DatasetInfo]:
    return [dataset.info for dataset in datasets]


def _select_documents(
    documents: list[dict[str, Any]],
    document_ids: list[str],
    filename_hints: list[str],
) -> list[dict[str, Any]]:
    """
    Resolve the structured documents requested by the planner.

    Explicit IDs take priority. Filename hints are then used to add
    additional matches.

    If neither is supplied, all ready structured documents are returned,
    allowing the structured reasoning layer to decide which tables matter.
    """

    by_id = {
        str(document["id"]): document
        for document in documents
    }

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    for document_id in document_ids:
        document = by_id.get(str(document_id))

        if document is None:
            continue

        selected.append(document)
        selected_ids.add(str(document["id"]))

    normalized_hints = [
        hint.strip().casefold()
        for hint in filename_hints
        if hint and hint.strip()
    ]

    if normalized_hints:
        for document in documents:
            document_id = str(document["id"])

            if document_id in selected_ids:
                continue

            filename = str(document.get("filename", "")).casefold()

            if any(
                _filename_matches_hint(filename, hint)
                for hint in normalized_hints
            ):
                selected.append(document)
                selected_ids.add(document_id)

    if selected:
        return selected

    # If the planner did not specify a particular structured document,
    # expose all ready tables. The SQL-generation model can select from
    # their schemas.
    if not document_ids and not normalized_hints:
        return documents

    return []


def _filename_matches_hint(
    filename: str,
    hint: str,
) -> bool:
    if not hint:
        return False

    if hint == filename:
        return True

    filename_stem = Path(filename).stem.casefold()
    hint_stem = Path(hint).stem.casefold()

    if hint_stem == filename_stem:
        return True

    return hint in filename or filename_stem in hint_stem


def _sample_values(
    series: pd.Series,
    limit: int = MAX_SCHEMA_SAMPLE_VALUES,
) -> list[Any]:
    """
    Return representative unique values without dumping large datasets
    into the model prompt.

    For low-cardinality columns, this naturally exposes categories.
    For high-cardinality columns, a small spread of values is returned.
    """

    if series.empty:
        return []

    try:
        unique = series.drop_duplicates()
    except Exception:
        unique = series

    if unique.empty:
        return []

    count = len(unique)

    if count <= limit:
        values = unique.tolist()
    else:
        # Spread samples across the column rather than only taking the
        # first N rows.
        positions = np.linspace(
            0,
            count - 1,
            num=limit,
            dtype=int,
        )

        values = [
            unique.iloc[int(position)]
            for position in positions
        ]

    result: list[Any] = []

    for value in values:
        safe = _json_safe(value)

        if isinstance(safe, str):
            safe = safe.strip()

            if len(safe) > MAX_STRING_SAMPLE_LENGTH:
                safe = safe[:MAX_STRING_SAMPLE_LENGTH] + "…"

        if safe not in result:
            result.append(safe)

    return result


def _friendly_dtype(series: pd.Series) -> str:
    """
    Give Gemini a simpler semantic dtype than raw Pandas dtype strings.
    """

    dtype = series.dtype

    if pd.api.types.is_bool_dtype(dtype):
        return "boolean"

    if pd.api.types.is_integer_dtype(dtype):
        return "integer"

    if pd.api.types.is_float_dtype(dtype):
        return "number"

    if pd.api.types.is_numeric_dtype(dtype):
        return "number"

    if pd.api.types.is_datetime64_any_dtype(dtype):
        return "datetime"

    if pd.api.types.is_timedelta64_dtype(dtype):
        return "duration"

    # Detect object/string columns that strongly resemble dates without
    # mutating the actual dataframe.
    if _looks_like_datetime(series):
        return "date_or_datetime_text"

    return "string"


def _looks_like_datetime(
    series: pd.Series,
    sample_size: int = 50,
) -> bool:
    """
    Conservative date-like detection used only for schema hints.

    It does NOT convert the user's data.
    """

    if not (
        pd.api.types.is_object_dtype(series.dtype)
        or pd.api.types.is_string_dtype(series.dtype)
    ):
        return False

    values = (
        series.dropna()
        .astype(str)
        .str.strip()
    )

    values = values[values != ""]

    if values.empty:
        return False

    sample = values.head(sample_size)

    # Avoid treating ordinary integers/IDs as dates.
    date_shape = sample.str.contains(
        r"[-/:]|[A-Za-z]{3,}",
        regex=True,
    )

    if float(date_shape.mean()) < 0.7:
        return False

    try:
        parsed = pd.to_datetime(
            sample,
            errors="coerce",
            format="mixed",
        )
    except (TypeError, ValueError):
        try:
            parsed = pd.to_datetime(
                sample,
                errors="coerce",
            )
        except Exception:
            return False

    success_rate = float(parsed.notna().mean())

    return success_rate >= 0.8


def _unique_column_names(
    columns: list[str],
) -> list[str]:
    """
    Make blank/duplicate column names safely addressable while preserving
    the original names as closely as possible.

    Example:
        ["Name", "Name", ""] ->
        ["Name", "Name__2", "column_3"]
    """

    result: list[str] = []
    counts: dict[str, int] = {}

    for index, raw_name in enumerate(columns, start=1):
        name = raw_name.strip()

        if not name:
            name = f"column_{index}"

        key = name.casefold()
        count = counts.get(key, 0) + 1
        counts[key] = count

        if count > 1:
            candidate = f"{name}__{count}"

            # Make sure the generated duplicate name itself does not
            # collide with another real column.
            while candidate.casefold() in {
                existing.casefold()
                for existing in result
            }:
                count += 1
                counts[key] = count
                candidate = f"{name}__{count}"

            name = candidate

        result.append(name)

    return result


def _normalized_file_type(
    document: dict[str, Any],
) -> str:
    file_type = str(
        document.get("file_type") or ""
    ).lower().strip().lstrip(".")

    if file_type:
        return file_type

    filename = str(document.get("filename") or "")

    return Path(filename).suffix.lower().lstrip(".")


def _json_safe(value: Any) -> Any:
    """
    Convert Pandas/Numpy scalar values into JSON-safe Python values.
    """

    if value is None:
        return None

    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, np.integer):
        return int(value)

    if isinstance(value, np.floating):
        number = float(value)

        if math.isnan(number) or math.isinf(number):
            return None

        return number

    if isinstance(value, np.bool_):
        return bool(value)

    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()

    if isinstance(value, pd.Timedelta):
        return str(value)

    if isinstance(value, bytes):
        return value.decode(
            "utf-8",
            errors="replace",
        )

    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return None

        return value

    return str(value)
