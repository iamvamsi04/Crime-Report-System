from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from app.dataset import LoadedDataset
from app.errors import AnalysisError
from app.models import StructuredResult

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Execution limits
# ---------------------------------------------------------------------------

DEFAULT_MAX_RESULT_ROWS = 200
MAX_SQL_LENGTH = 50_000


# ---------------------------------------------------------------------------
# SQL safety
# ---------------------------------------------------------------------------

# Only SELECT-style analytical queries are allowed. WITH is permitted because
# complex analysis often needs CTEs.
ALLOWED_START_KEYWORDS = {"SELECT", "WITH"}

# These keywords/statements are never needed for document analysis and can
# mutate state, access external resources, install extensions, or otherwise
# escape the intended in-memory analytical boundary.
FORBIDDEN_KEYWORDS = {
    "ALTER",
    "ATTACH",
    "CALL",
    "COPY",
    "CREATE",
    "DELETE",
    "DETACH",
    "DROP",
    "EXPORT",
    "IMPORT",
    "INSERT",
    "INSTALL",
    "LOAD",
    "MERGE",
    "PRAGMA",
    "REPLACE",
    "SET",
    "TRUNCATE",
    "UPDATE",
    "VACUUM",
}

# DuckDB can read files directly through table functions. The LLM must never
# use those functions. Uploaded files are loaded by dataset.py and registered
# as in-memory relations instead.
FORBIDDEN_FUNCTIONS = {
    "read_csv",
    "read_csv_auto",
    "read_json",
    "read_json_auto",
    "read_ndjson",
    "read_parquet",
    "parquet_scan",
    "csv_scan",
    "json_scan",
    "sqlite_scan",
    "postgres_scan",
    "mysql_scan",
    "delta_scan",
    "iceberg_scan",
    "httpfs",
}

# Additional strings associated with filesystem/network/extension access.
FORBIDDEN_PATTERNS = (
    r"\bhttps?://",
    r"\bs3://",
    r"\bgcs://",
    r"\baz://",
    r"\bfile://",
)

SQL_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
SQL_COMMENT_LINE_RE = re.compile(r"--[^\r\n]*")
WORD_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_structured_query(
    sql: str,
    datasets: list[LoadedDataset],
    *,
    max_result_rows: int = DEFAULT_MAX_RESULT_ROWS,
    explanation: str = "",
) -> StructuredResult:
    """
    Validate and execute a generated analytical SQL query.

    Security model:
        - DuckDB uses an in-memory connection.
        - Only application-loaded DataFrames are registered.
        - Only SELECT/WITH queries are accepted.
        - Multiple statements are rejected.
        - Mutating/admin/extension statements are rejected.
        - Direct file/network readers are rejected.
        - Referenced tables must be registered datasets.
        - External access is disabled at the DuckDB configuration level
          where supported.

    The returned StructuredResult is verified computational evidence.
    """

    if not datasets:
        raise AnalysisError(
            "No structured datasets were available for analysis."
        )

    max_result_rows = max(1, int(max_result_rows))

    normalized_sql = validate_sql(
        sql=sql,
        allowed_tables={
            dataset.table_name
            for dataset in datasets
        },
    )

    connection: duckdb.DuckDBPyConnection | None = None

    try:
        connection = _create_connection()

        for dataset in datasets:
            connection.register(
                dataset.table_name,
                dataset.dataframe,
            )

        # Do not rewrite the model's SQL to inject LIMIT because doing so can
        # change semantics for some analytical queries. Instead, execute the
        # exact validated query and truncate only the materialized result sent
        # to the answer model/frontend.
        result_frame = connection.execute(normalized_sql).fetchdf()

    except AnalysisError:
        raise

    except Exception as exc:
        log.warning(
            "structured_query_execution_failed error=%s sql=%s",
            _safe_error_text(exc),
            _sql_for_log(normalized_sql),
        )

        # This message is intentionally useful to the query-repair loop.
        # It contains the database error but not physical upload paths.
        raise AnalysisError(
            "Structured query execution failed: "
            f"{_safe_error_text(exc)}"
        ) from exc

    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                log.exception("duckdb_connection_close_failed")

    total_rows = len(result_frame)
    truncated = total_rows > max_result_rows

    visible_frame = result_frame.head(max_result_rows)

    rows = dataframe_records(visible_frame)
    columns = [
        str(column)
        for column in visible_frame.columns
    ]

    source_document_ids = [
        dataset.document_id
        for dataset in datasets
    ]

    source_filenames = [
        dataset.filename
        for dataset in datasets
    ]

    return StructuredResult(
        sql=normalized_sql,
        columns=columns,
        rows=rows,
        row_count=total_rows,
        truncated=truncated,
        source_document_ids=source_document_ids,
        source_filenames=source_filenames,
        explanation=explanation,
        result_text=format_structured_result(
            columns=columns,
            rows=rows,
            total_rows=total_rows,
            truncated=truncated,
        ),
    )


def validate_sql(
    sql: str,
    allowed_tables: set[str],
) -> str:
    """
    Validate generated SQL before DuckDB sees it.

    This is deliberately restrictive. The analytical model receives registered
    table names and should only query those tables.

    Raises AnalysisError when the query is unsafe or invalid.
    """

    if not isinstance(sql, str):
        raise AnalysisError(
            "Generated structured query was not valid SQL text."
        )

    sql = sql.strip()

    if not sql:
        raise AnalysisError(
            "Generated structured query was empty."
        )

    if len(sql) > MAX_SQL_LENGTH:
        raise AnalysisError(
            "Generated structured query exceeded the allowed size."
        )

    without_comments = _strip_sql_comments(sql).strip()

    if not without_comments:
        raise AnalysisError(
            "Generated structured query was empty."
        )

    # Reject multiple SQL statements.
    statements = _split_statements(without_comments)

    if len(statements) != 1:
        raise AnalysisError(
            "Only one read-only analytical SQL statement is allowed."
        )

    statement = statements[0].strip()

    first_keyword = _first_keyword(statement)

    if first_keyword not in ALLOWED_START_KEYWORDS:
        raise AnalysisError(
            "Only SELECT or WITH analytical queries are allowed."
        )

    sanitized = _remove_string_literals(statement)

    _reject_forbidden_keywords(sanitized)
    _reject_forbidden_functions(sanitized)
    _reject_forbidden_patterns(sanitized)

    referenced_tables = extract_referenced_tables(sanitized)

    normalized_allowed = {
        table.casefold()
        for table in allowed_tables
    }

    # CTE names are local temporary relations and therefore also legal.
    cte_names = extract_cte_names(sanitized)

    unknown_tables = sorted(
        table
        for table in referenced_tables
        if table.casefold() not in normalized_allowed
        and table.casefold() not in cte_names
    )

    if unknown_tables:
        raise AnalysisError(
            "The generated query referenced unavailable table(s): "
            + ", ".join(unknown_tables)
        )

    # Preserve the original SQL rather than the comment-stripped version so
    # quoted identifiers and formatting remain untouched.
    return sql.rstrip().rstrip(";").strip()


def extract_referenced_tables(sql: str) -> set[str]:
    """
    Extract relations following FROM/JOIN.

    This is a safety check, not a complete SQL parser. DuckDB itself performs
    the authoritative syntax/binding validation during execution.

    The generated SQL is additionally constrained by keyword/function checks
    and an in-memory DuckDB connection.
    """

    pattern = re.compile(
        r"""
        \b(?:FROM|JOIN)\s+
        (?:
            ["`]([^"`]+)["`]
            |
            ([A-Za-z_][A-Za-z0-9_]*)
        )
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    tables: set[str] = set()

    for match in pattern.finditer(sql):
        name = match.group(1) or match.group(2)

        if not name:
            continue

        name = name.strip()

        # A table function such as read_csv(...) is handled separately by the
        # forbidden-function validator.
        if name:
            tables.add(name)

    return tables


def extract_cte_names(sql: str) -> set[str]:
    """
    Extract CTE names from WITH clauses.

    Handles the common generated forms:
        WITH totals AS (...)
        WITH a AS (...), b AS (...)

    Recursive CTEs are also recognized.
    """

    names: set[str] = set()

    # Every CTE definition has "<identifier> AS (" after WITH or a comma.
    pattern = re.compile(
        r"""
        (?:\bWITH\s+(?:RECURSIVE\s+)?|,)
        \s*
        (?:
            "([^"]+)"
            |
            `([^`]+)`
            |
            ([A-Za-z_][A-Za-z0-9_]*)
        )
        \s*
        (?:\([^)]*\)\s*)?
        AS\s*\(
        """,
        re.IGNORECASE | re.VERBOSE,
    )

    for match in pattern.finditer(sql):
        name = (
            match.group(1)
            or match.group(2)
            or match.group(3)
        )

        if name:
            names.add(name.casefold())

    return names


def dataframe_records(
    frame: pd.DataFrame,
) -> list[dict[str, Any]]:
    """
    Convert a DuckDB/Pandas result into JSON-safe records.
    """

    records: list[dict[str, Any]] = []

    for raw_record in frame.to_dict(orient="records"):
        record: dict[str, Any] = {}

        for key, value in raw_record.items():
            record[str(key)] = _json_safe(value)

        records.append(record)

    return records


def format_structured_result(
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    total_rows: int,
    truncated: bool,
) -> str:
    """
    Produce a compact grounded representation for the final answer model.

    This is intentionally deterministic. Gemini explains the result but does
    not recalculate it.
    """

    if not columns:
        return (
            "The structured query completed successfully but returned "
            "no columns."
        )

    if not rows:
        return (
            "The structured query completed successfully and returned "
            "0 rows."
        )

    lines: list[str] = [
        f"Verified structured result: {total_rows} row(s).",
        "",
        "Columns: " + ", ".join(columns),
        "",
    ]

    for index, row in enumerate(rows, start=1):
        values = [
            f"{column}={_display_value(row.get(column))}"
            for column in columns
        ]

        lines.append(
            f"Row {index}: " + "; ".join(values)
        )

    if truncated:
        lines.extend(
            [
                "",
                (
                    "Result display was truncated. "
                    f"Only the first {len(rows)} of {total_rows} rows "
                    "are shown."
                ),
            ]
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# DuckDB connection
# ---------------------------------------------------------------------------


def _create_connection() -> duckdb.DuckDBPyConnection:
    """
    Create an isolated in-memory DuckDB connection.

    We also attempt to disable external access at the engine level. This is
    defense-in-depth on top of SQL validation.
    """

    try:
        connection = duckdb.connect(
            database=":memory:",
            config={
                "enable_external_access": "false",
                "allow_unsigned_extensions": "false",
            },
        )
    except Exception:
        # Some DuckDB versions may not accept every configuration option at
        # connect time. Fall back to the in-memory connection and apply the
        # settings individually where supported.
        connection = duckdb.connect(database=":memory:")

        _try_setting(
            connection,
            "SET enable_external_access = false",
        )

        _try_setting(
            connection,
            "SET allow_unsigned_extensions = false",
        )

    # Keep progress UI and other engine output out of server logs.
    _try_setting(
        connection,
        "SET enable_progress_bar = false",
    )

    return connection


def _try_setting(
    connection: duckdb.DuckDBPyConnection,
    statement: str,
) -> None:
    try:
        connection.execute(statement)
    except Exception:
        # Version-specific hardening settings should not prevent startup.
        # SQL validation remains the primary application boundary.
        log.debug(
            "duckdb_setting_not_supported statement=%s",
            statement,
        )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _reject_forbidden_keywords(sql: str) -> None:
    words = {
        match.group(0).upper()
        for match in WORD_RE.finditer(sql)
    }

    found = sorted(
        words.intersection(FORBIDDEN_KEYWORDS)
    )

    if found:
        raise AnalysisError(
            "Generated query contains prohibited SQL operation(s): "
            + ", ".join(found)
        )


def _reject_forbidden_functions(sql: str) -> None:
    lowered = sql.casefold()

    for function_name in FORBIDDEN_FUNCTIONS:
        pattern = (
            r"\b"
            + re.escape(function_name.casefold())
            + r"\s*\("
        )

        if re.search(pattern, lowered):
            raise AnalysisError(
                "Generated query attempted to use a prohibited "
                f"external function: {function_name}"
            )


def _reject_forbidden_patterns(sql: str) -> None:
    lowered = sql.casefold()

    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            raise AnalysisError(
                "Generated query attempted to access an external resource."
            )


def _first_keyword(sql: str) -> str:
    match = WORD_RE.search(sql)

    if not match:
        return ""

    return match.group(0).upper()


def _strip_sql_comments(sql: str) -> str:
    sql = SQL_COMMENT_BLOCK_RE.sub(" ", sql)
    sql = SQL_COMMENT_LINE_RE.sub(" ", sql)

    return sql


def _remove_string_literals(sql: str) -> str:
    """
    Remove single-quoted string contents before keyword safety checks.

    Example:
        WHERE description = 'DROP in revenue'

    should not be rejected merely because a data value contains "DROP".

    Quoted identifiers remain because their names can still be relevant to
    relation validation.
    """

    result: list[str] = []

    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char != "'":
            result.append(char)
            index += 1
            continue

        result.append("''")
        index += 1

        while index < length:
            if sql[index] != "'":
                index += 1
                continue

            # SQL escapes a quote inside a string as two single quotes.
            if (
                index + 1 < length
                and sql[index + 1] == "'"
            ):
                index += 2
                continue

            index += 1
            break

    return "".join(result)


def _split_statements(sql: str) -> list[str]:
    """
    Split on semicolons outside strings and quoted identifiers.

    Generated analysis must contain exactly one SQL statement.
    """

    statements: list[str] = []
    buffer: list[str] = []

    quote: str | None = None
    index = 0

    while index < len(sql):
        char = sql[index]

        if quote is not None:
            buffer.append(char)

            if char == quote:
                # SQL escaped single/double quote.
                if (
                    index + 1 < len(sql)
                    and sql[index + 1] == quote
                ):
                    buffer.append(sql[index + 1])
                    index += 2
                    continue

                quote = None

            index += 1
            continue

        if char in {"'", '"', "`"}:
            quote = char
            buffer.append(char)
            index += 1
            continue

        if char == ";":
            statement = "".join(buffer).strip()

            if statement:
                statements.append(statement)

            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    final = "".join(buffer).strip()

    if final:
        statements.append(final)

    return statements


# ---------------------------------------------------------------------------
# Result/error helpers
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    if value is None:
        return None

    try:
        missing = pd.isna(value)

        if isinstance(missing, (bool, np.bool_)) and missing:
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

    if isinstance(
        value,
        (
            pd.Timestamp,
            datetime,
            date,
        ),
    ):
        return value.isoformat()

    if isinstance(value, pd.Timedelta):
        return str(value)

    if isinstance(value, bytes):
        return value.decode(
            "utf-8",
            errors="replace",
        )

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None

        return value

    if isinstance(value, (str, int, bool)):
        return value

    return str(value)


def _display_value(value: Any) -> str:
    if value is None:
        return "NULL"

    if isinstance(value, str):
        # Avoid huge text cells consuming the final-answer context.
        value = value.replace("\r", " ").replace("\n", " ")

        if len(value) > 500:
            return repr(value[:500] + "…")

        return repr(value)

    return str(value)


def _safe_error_text(
    exc: Exception,
) -> str:
    """
    Keep useful DuckDB errors for automatic query repair while avoiding
    accidental disclosure of local filesystem paths.
    """

    text = str(exc).strip()

    if not text:
        return exc.__class__.__name__

    # Unix-like absolute paths.
    text = re.sub(
        r"(?<![\w])/(?:[^/\s:]+/)+[^/\s:]+",
        "<local-path>",
        text,
    )

    # Windows absolute paths.
    text = re.sub(
        r"[A-Za-z]:\\(?:[^\\\r\n]+\\)*[^\\\r\n]*",
        "<local-path>",
        text,
    )

    if len(text) > 2_000:
        text = text[:2_000] + "…"

    return text


def _sql_for_log(
    sql: str,
) -> str:
    compact = re.sub(
        r"\s+",
        " ",
        sql,
    ).strip()

    if len(compact) > 2_000:
        compact = compact[:2_000] + "…"

    return compact
