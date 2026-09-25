from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from app import database
from app.llm import generate_structured_response
from app.models import (
    CSVColumn,
    CSVProfile,
    CSVQueryResult,
    Document,
)

log = logging.getLogger(__name__)

MAX_RESULT_ROWS = 1000

FORBIDDEN_SQL = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "COPY",
    "EXPORT",
    "IMPORT",
    "ATTACH",
    "DETACH",
    "INSTALL",
    "LOAD",
    "SET",
    "RESET",
    "CALL",
}


async def profile_csv_document(document: Document) -> None:
    """Read a tabular document and save its normalized schema profile."""

    if document.file_type not in {"csv", "excel"}:
        raise ValueError(
            "Only CSV and Excel files can be profiled."
        )

    path = Path(document.path)

    if not path.exists():
        raise FileNotFoundError(
            f"Data file not found: {path}"
        )

    dataframe = load_dataframe(
        path=path,
        file_type=document.file_type,
    )

    if dataframe.empty:
        raise ValueError(
            "The uploaded data file contains no rows."
        )

    dataframe = normalize_dataframe_columns(
        dataframe
    )

    profile = build_csv_profile(
        document=document,
        dataframe=dataframe,
    )

    database.save_csv_profile(
        document_id=document.id,
        profile=profile.model_dump(mode="json"),
    )


def load_dataframe(
    path: Path,
    file_type: str,
) -> pd.DataFrame:
    """Load CSV or Excel data into a pandas DataFrame."""

    if file_type == "csv":
        dataframe = pd.read_csv(path)

    elif file_type == "excel":
        dataframe = pd.read_excel(
            path,
            sheet_name=0,
        )

    else:
        raise ValueError(
            f"Unsupported tabular file type: {file_type}"
        )

    return normalize_dataframe_columns(
        dataframe
    )


def normalize_column_name(name: Any) -> str:
    """
    Normalize a column name so that whitespace differences
    in the original file cannot break SQL generation.
    """

    normalized = str(name).strip()

    # Collapse repeated whitespace inside a column name.
    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    )

    return normalized


def normalize_dataframe_columns(
    dataframe: pd.DataFrame,
) -> pd.DataFrame:
    """
    Normalize all DataFrame column names.

    Example:
        '  Units Sold ' -> 'Units Sold'
        '  Sales '      -> 'Sales'
    """

    dataframe = dataframe.copy()

    normalized_columns = [
        normalize_column_name(column)
        for column in dataframe.columns
    ]

    if len(normalized_columns) != len(
        set(normalized_columns)
    ):
        raise ValueError(
            "The data file contains duplicate column names "
            "after whitespace normalization."
        )

    dataframe.columns = normalized_columns

    return dataframe


def build_csv_profile(
    document: Document,
    dataframe: pd.DataFrame,
) -> CSVProfile:
    """Create a schema profile from a normalized DataFrame."""

    columns: list[CSVColumn] = []

    for column in dataframe.columns:
        series = dataframe[column]

        sample_values = (
            series
            .dropna()
            .head(5)
            .tolist()
        )

        normalized_samples = [
            normalize_value(value)
            for value in sample_values
        ]

        columns.append(
            CSVColumn(
                name=str(column),
                data_type=str(series.dtype),
                nullable=bool(series.isna().any()),
                sample_values=normalized_samples,
            )
        )

    return CSVProfile(
        document_id=document.id,
        filename=document.filename,
        row_count=len(dataframe),
        column_count=len(dataframe.columns),
        columns=columns,
        description=(
            "Profile generated from the uploaded "
            "tabular document."
        ),
    )


def normalize_value(value: Any) -> Any:
    """Convert pandas/numpy values into JSON-safe values."""

    if pd.isna(value):
        return None

    if isinstance(
        value,
        (str, int, float, bool),
    ):
        return value

    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass

    return str(value)


def get_profile(
    document_id: str,
) -> CSVProfile | None:
    """Retrieve and validate a saved CSV profile."""

    raw_profile = database.get_csv_profile(
        document_id
    )

    if raw_profile is None:
        return None

    try:
        return CSVProfile.model_validate(
            raw_profile
        )
    except Exception:
        log.exception(
            "Invalid CSV profile for document %s",
            document_id,
        )
        return None


async def generate_sql(
    question: str,
    profile: CSVProfile,
) -> str:
    """Generate a read-only DuckDB query from the CSV profile."""

    profile_json = json.dumps(
        profile.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
    )

    prompt = f"""
You generate DuckDB SQL for answering questions
about one uploaded tabular file.

You are given:
1. The user's question.
2. A profile describing the available columns.

Your job is to generate ONE read-only DuckDB SQL query.

Rules:
- Query only the table named `data`.
- Use ONLY columns that exist in the supplied profile.
- Column names must match the profile exactly.
- Do NOT add spaces before or after column names.
- Do NOT invent columns.
- Do NOT modify data.
- Do NOT create tables.
- Do NOT access external files.
- Do NOT use INSERT, UPDATE, DELETE, DROP, ALTER,
  COPY, EXPORT, IMPORT, ATTACH, DETACH,
  INSTALL, LOAD, SET, RESET, or CALL.
- Use DuckDB SQL syntax.
- For calculations, perform the calculation in SQL.
- For "highest", "lowest", "best", etc., explicitly
  order and limit the result when appropriate.
- Return only SQL.
- Do not include markdown fences.
- The query must answer the user's question directly.

IMPORTANT ENTITY INTERPRETATION RULE:

When the user mentions a specific value or entity without
explicitly naming the column, use the representative
column values in the profile to determine which column
contains that value.

For example, if the question is:

    total units sold for Montana

and the profile shows:

    Product -> Montana
    Country -> Canada, France, Germany
    Segment -> Midmarket, Government, Small Business, Enterprise, Channel Partners

then interpret Montana as a Product and use:

    WHERE "Product" = ' Montana '

Do NOT assume that an entity is a Country, Product,
Department, Region, or any other category merely from
the wording.


IMPORTANT:
The column names in the profile have already been
normalized by removing unnecessary leading/trailing
whitespace.

CSV PROFILE:
{profile_json}

USER QUESTION:
{question}
"""

    response = await generate_structured_response(
        prompt=prompt,
        schema={
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string"
                }
            },
            "required": ["sql"],
        },
    )

    sql = response.get("sql")

    if not isinstance(sql, str):
        raise ValueError(
            "The LLM did not return valid SQL."
        )

    sql = clean_sql(sql)

    validate_sql(
        sql=sql,
        profile=profile,
    )

    return sql


def clean_sql(sql: str) -> str:
    """Remove markdown fences and trailing semicolons."""

    sql = sql.strip()

    if sql.startswith("```"):
        sql = re.sub(
            r"^```(?:sql)?\s*",
            "",
            sql,
            flags=re.IGNORECASE,
        )

        sql = re.sub(
            r"\s*```$",
            "",
            sql,
        )

    sql = sql.strip()

    if sql.endswith(";"):
        sql = sql[:-1].strip()

    return sql


def validate_sql(
    sql: str,
    profile: CSVProfile,
) -> None:
    """Validate that generated SQL is safe and references known columns."""

    if not sql:
        raise ValueError(
            "Generated SQL is empty."
        )

    normalized = re.sub(
        r"\s+",
        " ",
        sql.strip(),
    )

    if ";" in normalized:
        raise ValueError(
            "Multiple SQL statements are not allowed."
        )

    first_keyword_match = re.match(
        r"^\s*([A-Za-z]+)",
        normalized,
    )

    if not first_keyword_match:
        raise ValueError(
            "Could not determine the SQL statement type."
        )

    first_keyword = (
        first_keyword_match
        .group(1)
        .upper()
    )

    if first_keyword not in {
        "SELECT",
        "WITH",
    }:
        raise ValueError(
            "Only SELECT or WITH queries are allowed."
        )

    upper_sql = normalized.upper()

    for keyword in FORBIDDEN_SQL:
        if re.search(
            rf"\b{re.escape(keyword)}\b",
            upper_sql,
        ):
            raise ValueError(
                f"Forbidden SQL operation detected: {keyword}"
            )

    if not re.search(
        r"\bdata\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        raise ValueError(
            "Generated SQL must query the `data` table."
        )

    validate_columns(
        sql=sql,
        profile=profile,
    )


def validate_columns(
    sql: str,
    profile: CSVProfile,
) -> None:
    """
    Validate quoted column identifiers against the normalized profile.

    The table name `data` is also allowed.
    """

    available_columns = {
        normalize_column_name(column.name).lower()
        for column in profile.columns
    }

    quoted_identifiers = re.findall(
        r'"([^"]+)"',
        sql,
    )

    for identifier in quoted_identifiers:
        normalized_identifier = (
            normalize_column_name(identifier)
        )

        if normalized_identifier.lower() == "data":
            continue

        if (
            normalized_identifier.lower()
            not in available_columns
        ):
            raise ValueError(
                f"Unknown column referenced: {identifier}"
            )


def execute_duckdb_query(
    document: Document,
    sql: str,
) -> CSVQueryResult:
    """
    Execute a validated query against a normalized DataFrame.

    The same DataFrame is used for profiling and DuckDB execution.
    This guarantees that the column names are identical in both places.
    """

    path = Path(document.path)



    if not path.exists():
        raise FileNotFoundError(
            f"Data file not found: {path}"
        )

    profile = get_profile(
        document.id
    )

    if profile is None:
        raise ValueError(
            "CSV profile is unavailable."
        )

    sql = normalize_sql_identifiers(
    sql=sql,
    profile=profile,
)

    validate_sql(
        sql=sql,
        profile=profile,
    )

    dataframe = load_dataframe(
        path=path,
        file_type=document.file_type,
    )

    connection = duckdb.connect(
        database=":memory:"
    )

    try:

        connection.register(
            "dataframe",
            dataframe,
        )

        connection.execute(
            """
            CREATE VIEW data AS
            SELECT *
            FROM dataframe
            """
        )

        result = connection.execute(
            sql
        )

        rows = result.fetchmany(
            MAX_RESULT_ROWS
        )

        columns = [
            description[0]
            for description in result.description
        ]

        normalized_rows = [
            {
                column: normalize_value(value)
                for column, value in zip(
                    columns,
                    row,
                )
            }
            for row in rows
        ]

        return CSVQueryResult(
            document_id=document.id,
            filename=document.filename,
            sql=sql,
            columns=columns,
            rows=normalized_rows,
            row_count=len(
                normalized_rows
            ),
        )

    finally:
        connection.close()
def normalize_sql_identifiers(
    sql: str,
    profile: CSVProfile,
) -> str:
    """
    Replace whitespace-variant quoted column names with
    their normalized profile names.

    Example:
        " Units Sold " -> "Units Sold"
        " Product "    -> "Product"
    """

    normalized_sql = sql

    for column in profile.columns:
        original_name = column.name
        normalized_name = normalize_column_name(
            original_name
        )

        if original_name != normalized_name:
            normalized_sql = normalized_sql.replace(
                f'"{original_name}"',
                f'"{normalized_name}"',
            )

    return normalized_sql

async def answer_csv_question(
    question: str,
    document: Document,
) -> CSVQueryResult:
    """Generate and execute a SQL query for a tabular question."""

    profile = get_profile(
        document.id
    )

    if profile is None:
        raise ValueError(
            "CSV profile is unavailable."
        )

    sql = await generate_sql(
        question=question,
        profile=profile,
    )

    result = execute_duckdb_query(
        document=document,
        sql=sql,
    )

    log.info(
        "DuckDB query executed for %s: %s",
        document.filename,
        sql,
    )

    return result
