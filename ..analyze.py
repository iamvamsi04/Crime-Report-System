from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import duckdb

from app.errors import AnalysisError, NotFoundError
from app.gemini import GeminiService
from app.models import AnalysisResult, PlanOp, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)


ANALYSIS_OPS = {
    "load_csv",
    "sum",
    "average",
    "min",
    "max",
    "count",
    "sort",
    "rank",
    "filter",
    "groupby",
    "percentage_change",
    "yoy",
    "compare",
}


SQL_GENERATION_SYSTEM = """
You generate ONE read-only DuckDB SQL query for an uploaded CSV or Excel
dataset.

The dataset is already exposed to DuckDB as a table named `dataset`.

Return JSON only using exactly this structure:

{
  "sql": "SELECT ...",
  "operation": "sum"
}

Rules:

1. Generate exactly ONE SQL statement.

2. The SQL MUST be read-only.

3. The SQL MUST query ONLY the table:
   dataset

4. NEVER reference:
   - files
   - filesystem paths
   - other tables
   - SQLite
   - information_schema
   - read_csv
   - read_csv_auto
   - read_xlsx
   - parquet_scan
   - glob
   - attach
   - install
   - load
   - copy

5. NEVER use:
   INSERT
   UPDATE
   DELETE
   MERGE
   CREATE
   DROP
   ALTER
   TRUNCATE
   REPLACE
   VACUUM
   EXPORT
   IMPORT
   ATTACH
   DETACH
   INSTALL
   LOAD
   COPY

6. The result must be directly useful to the application.

7. For a numeric aggregation, return a single row with a clear column
   named `result`.

8. For a single matching row asking for a numeric value, SUM is acceptable
   when the requested column represents a numeric measure.

9. For percentage change, calculate the percentage in SQL.

10. For year-over-year calculations, calculate the requested result in SQL.

11. For ranking/sorting/grouping questions, return a result table.

12. Never invent column names. Use only columns supplied in the schema.

13. Preserve exact column names by using double quotes when necessary.

14. String comparisons should generally be case-insensitive when appropriate.

15. Date comparisons should use DuckDB date conversion when necessary.

16. Do not perform calculations outside SQL.

17. If the requested operation cannot be represented safely using the supplied
    schema, return a query that produces no rows rather than inventing data.

18. Do not add markdown fences around the SQL.

19. The SQL must begin with SELECT or WITH.
"""


def needs_analysis(plan: QueryPlan) -> bool:
    """
    Return True when the plan contains a structured CSV/Excel analysis
    operation.

    Retrieval-only document questions do not require DuckDB.
    """
    return any(
        op.op in ANALYSIS_OPS and op.op != "retrieve"
        for op in plan.operations
    )


def run_analysis(
    plan: QueryPlan,
    store: Storage,
    gemini: GeminiService,
    settings: Any | None = None,
) -> list[AnalysisResult]:
    """
    Execute CSV/Excel analysis using DuckDB.

    The original uploaded file remains in data/uploads.
    SQLite remains responsible for document metadata and csv_profile.

    Flow:

        QueryPlan
            ↓
        csv_profile from SQLite
            ↓
        Gemini generates SQL
            ↓
        SQL validation
            ↓
        DuckDB exposes original file as `dataset`
            ↓
        SQL execution
            ↓
        AnalysisResult
    """

    document = _choose_document(
        plan=plan,
        store=store,
    )

    if document is None:
        raise AnalysisError(
            "No CSV or Excel dataset is available for this calculation."
        )

    document_id = document["id"]
    source_file = document["filename"]
    file_type = document["file_type"]

    try:
        path = store.document_file(document_id)
    except NotFoundError:
        raise
    except Exception as exc:
        raise AnalysisError(
            "The CSV or Excel dataset could not be located."
        ) from exc

    if not path.exists():
        raise AnalysisError(
            "The uploaded CSV or Excel dataset could not be found."
        )

    profile = _load_profile(document)

    question_context = _build_sql_context(
        plan=plan,
        document=document,
        profile=profile,
    )

    sql_payload = gemini.generate_json(
        SQL_GENERATION_SYSTEM,
        json.dumps(
            question_context,
            default=str,
        ),
    )

    sql, generated_operation = _parse_generated_sql(
        sql_payload
    )

    _validate_sql(sql)

    log.info(
        "duckdb_analysis document=%s operation=%s sql=%s",
        source_file,
        generated_operation,
        sql,
    )

    connection = None

    try:
        connection = duckdb.connect(database=":memory:")

        _register_dataset(
            connection=connection,
            path=path,
            file_type=file_type,
        )

        rows = connection.execute(sql).fetchall()
        columns = [
            description[0]
            for description in connection.description
        ]

    except Exception as exc:
        log.exception(
            "duckdb_analysis_failed file=%s",
            source_file,
        )

        raise AnalysisError(
            "The dataset analysis failed while executing the generated SQL."
        ) from exc

    finally:
        if connection is not None:
            connection.close()

    return [
        _build_analysis_result(
            operation=generated_operation,
            sql=sql,
            rows=rows,
            columns=columns,
            source_file=source_file,
        )
    ]


# ============================================================
# DOCUMENT SELECTION
# ============================================================


def _choose_document(
    *,
    plan: QueryPlan,
    store: Storage,
) -> dict[str, Any] | None:
    documents = [
        document
        for document in store.list_documents(
            ready_only=True
        )
        if document["file_type"] in {"csv", "excel"}
    ]

    if not documents:
        return None

    # --------------------------------------------------------
    # Explicit document ID
    # --------------------------------------------------------

    for document_id in plan.document_ids:
        for document in documents:
            if document["id"] == document_id:
                return document

    # --------------------------------------------------------
    # Filename hint
    # --------------------------------------------------------

    for hint in plan.document_hints:
        if not hint:
            continue

        hint_lower = hint.casefold()

        for document in documents:
            if hint_lower in document["filename"].casefold():
                return document

    # --------------------------------------------------------
    # Operation filename hint
    # --------------------------------------------------------

    for operation in plan.operations:
        if not operation.filename_hint:
            continue

        hint_lower = operation.filename_hint.casefold()

        for document in documents:
            if hint_lower in document["filename"].casefold():
                return document

    # --------------------------------------------------------
    # Fallback
    # --------------------------------------------------------

    return documents[0]


# ============================================================
# CSV PROFILE
# ============================================================


def _load_profile(
    document: dict[str, Any],
) -> dict[str, Any]:
    raw = document.get("csv_profile")

    if not raw:
        return {}

    if isinstance(raw, dict):
        return raw

    try:
        value = json.loads(raw)

        if isinstance(value, dict):
            return value

    except (json.JSONDecodeError, TypeError):
        log.warning(
            "invalid_csv_profile document=%s",
            document.get("id"),
        )

    return {}


# ============================================================
# SQL GENERATION CONTEXT
# ============================================================


def _build_sql_context(
    *,
    plan: QueryPlan,
    document: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """
    Give Gemini the actual SQLite csv_profile rather than asking it
    to discover the dataset blindly.
    """

    operations = [
        operation.model_dump(
            by_alias=True
        )
        for operation in plan.operations
        if operation.op != "retrieve"
    ]

    return {
        "dataset": {
            "document_id": document["id"],
            "filename": document["filename"],
            "file_type": document["file_type"],
            "table_name": "dataset",
            "profile": {
                "columns": profile.get("columns") or [],
                "dtypes": profile.get("dtypes") or {},
                "row_count": profile.get("row_count"),
                "numeric_columns": profile.get("numeric_columns") or [],
                "year_columns": profile.get("year_columns") or [],
                "entity_columns": profile.get("entity_columns") or [],
                "categorical_columns": profile.get("categorical_columns") or [],
                "null_counts": profile.get("null_counts") or {},
            },
        },
        "query_plan": {
            "intent": plan.intent.value,
            "conversation_intent": plan.conversation_intent,
            "is_follow_up": plan.is_follow_up,
            "entities": plan.entities,
            "metrics": plan.metrics,
            "years": plan.years,
            "filters": plan.filters,
            "operations": operations,
            "resolved_question": plan.resolved_question,
            "retrieval_query": plan.retrieval_query,
        },
    }


# ============================================================
# DUCKDB DATASET REGISTRATION
# ============================================================


def _register_dataset(
    *,
    connection: Any,
    path: Path,
    file_type: str,
) -> None:
    """
    Expose the original uploaded file as:

        dataset

    No data is copied into SQLite.
    """

    escaped_path = _escape_sql_string(
        str(path)
    )

    if file_type == "csv":
        sql = (
            "CREATE VIEW dataset AS "
            f"SELECT * FROM read_csv_auto('{escaped_path}', "
            "header=true, "
            "sample_size=-1)"
        )

    elif file_type == "excel":
        # DuckDB's Excel reader is provided by the spatial extension
        # in supported DuckDB releases.
        try:
            connection.execute(
                "LOAD excel"
            )
        except Exception:
            try:
                connection.execute(
                    "INSTALL excel"
                )
                connection.execute(
                    "LOAD excel"
                )
            except Exception as exc:
                raise AnalysisError(
                    "DuckDB could not load its Excel reader. "
                    "Please install a DuckDB version with Excel support."
                ) from exc

        sql = (
            "CREATE VIEW dataset AS "
            f"SELECT * FROM read_xlsx('{escaped_path}', "
            "header=true)"
        )

    else:
        raise AnalysisError(
            f"Unsupported structured document type '{file_type}'."
        )

    connection.execute(sql)


def _escape_sql_string(
    value: str,
) -> str:
    return value.replace(
        "'",
        "''",
    )


# ============================================================
# GENERATED SQL PARSING
# ============================================================


def _parse_generated_sql(
    payload: Any,
) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise AnalysisError(
            "Gemini returned an invalid SQL analysis response."
        )

    sql = payload.get("sql")
    operation = payload.get("operation") or "analysis"

    if not isinstance(sql, str):
        raise AnalysisError(
            "Gemini did not return a SQL query."
        )

    sql = sql.strip()

    if not sql:
        raise AnalysisError(
            "Gemini returned an empty SQL query."
        )

    return sql, str(operation)


# ============================================================
# SQL SAFETY VALIDATION
# ============================================================


def _validate_sql(
    sql: str,
) -> None:
    """
    Ensure Gemini can only produce a single read-only query
    against the already-registered `dataset` view.
    """

    normalized = sql.strip()

    # --------------------------------------------------------
    # No markdown
    # --------------------------------------------------------

    if "```" in normalized:
        raise AnalysisError(
            "Generated SQL contained markdown formatting."
        )

    # --------------------------------------------------------
    # Exactly one statement
    # --------------------------------------------------------

    if ";" in normalized.rstrip(";"):
        raise AnalysisError(
            "Generated SQL contains multiple statements."
        )

    normalized = normalized.rstrip(";").strip()

    # --------------------------------------------------------
    # Must be SELECT / WITH
    # --------------------------------------------------------

    if not re.match(
        r"^(select|with)\b",
        normalized,
        re.IGNORECASE,
    ):
        raise AnalysisError(
            "Generated SQL is not a read-only SELECT query."
        )

    # --------------------------------------------------------
    # Dangerous SQL keywords
    # --------------------------------------------------------

    forbidden_keywords = (
        "insert",
        "update",
        "delete",
        "merge",
        "create",
        "drop",
        "alter",
        "truncate",
        "replace",
        "vacuum",
        "export",
        "import",
        "attach",
        "detach",
        "install",
        "load",
        "copy",
        "call",
    )

    for keyword in forbidden_keywords:
        if re.search(
            rf"\b{re.escape(keyword)}\b",
            normalized,
            re.IGNORECASE,
        ):
            raise AnalysisError(
                "Generated SQL contains a prohibited SQL operation."
            )

    # --------------------------------------------------------
    # Prevent direct filesystem access from generated SQL
    # --------------------------------------------------------

    forbidden_functions = (
        "read_csv",
        "read_csv_auto",
        "read_xlsx",
        "read_parquet",
        "parquet_scan",
        "glob",
    )

    for function_name in forbidden_functions:
        if re.search(
            rf"\b{re.escape(function_name)}\s*\(",
            normalized,
            re.IGNORECASE,
        ):
            raise AnalysisError(
                "Generated SQL attempted to access the filesystem directly."
            )

    # --------------------------------------------------------
    # Prevent external database access
    # --------------------------------------------------------

    if re.search(
        r"\binformation_schema\b",
        normalized,
        re.IGNORECASE,
    ):
        raise AnalysisError(
            "Generated SQL attempted to access database metadata."
        )

    if re.search(
        r"\bpragma\b",
        normalized,
        re.IGNORECASE,
    ):
        raise AnalysisError(
            "Generated SQL contains a prohibited PRAGMA statement."
        )

    # --------------------------------------------------------
    # Only the registered dataset should be queried.
    #
    # This is deliberately conservative. The generated query may
    # use CTEs and aliases, but the physical source is `dataset`.
    # --------------------------------------------------------

    if not re.search(
        r"\bdataset\b",
        normalized,
        re.IGNORECASE,
    ):
        raise AnalysisError(
            "Generated SQL does not reference the uploaded dataset."
        )

    # --------------------------------------------------------
    # Reject obvious external URI/file references.
    # --------------------------------------------------------

    if re.search(
        r"(https?://|file://|[A-Za-z]:\\)",
        normalized,
        re.IGNORECASE,
    ):
        raise AnalysisError(
            "Generated SQL contains an external file or network reference."
        )


# ============================================================
# RESULT CONVERSION
# ============================================================


def _build_analysis_result(
    *,
    operation: str,
    sql: str,
    rows: list[tuple[Any, ...]],
    columns: list[str],
    source_file: str,
) -> AnalysisResult:
    """
    Convert DuckDB's result into the existing AnalysisResult model.

    This preserves the contract used by chat.py and evidence.py.
    """

    table = _rows_to_table(
        rows=rows,
        columns=columns,
    )

    value = None

    # --------------------------------------------------------
    # Scalar result
    # --------------------------------------------------------

    if len(rows) == 1 and len(columns) == 1:
        value = _clean_value(
            rows[0][0]
        )

    # --------------------------------------------------------
    # Common generated aggregation format:
    #
    # SELECT SUM(...) AS result
    # --------------------------------------------------------

    elif len(rows) == 1:
        result_index = _find_result_column(
            columns
        )

        if result_index is not None:
            value = _clean_value(
                rows[0][result_index]
            )

    # --------------------------------------------------------
    # Result metadata
    # --------------------------------------------------------

    formula = sql

    return AnalysisResult(
        operation=operation,
        value=value,
        table=table if table else None,
        inputs={
            "sql": sql,
        },
        formula=formula,
        source_file=source_file,
        rows_used=len(rows),
        columns_used=columns,
    )


def _rows_to_table(
    *,
    rows: list[tuple[Any, ...]],
    columns: list[str],
) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []

    for row in rows:
        record: dict[str, Any] = {}

        for index, column in enumerate(columns):
            value = row[index]

            record[column] = _clean_value(
                value
            )

        table.append(record)

    return table


def _find_result_column(
    columns: list[str],
) -> int | None:
    preferred = {
        "result",
        "value",
        "total",
        "sum",
        "average",
        "avg",
        "mean",
        "minimum",
        "maximum",
        "count",
        "percentage_change",
        "percentage",
        "growth",
    }

    for index, column in enumerate(columns):
        if column.casefold().strip() in preferred:
            return index

    return None


# ============================================================
# VALUE NORMALIZATION
# ============================================================


def _clean_value(
    value: Any,
) -> Any:
    if value is None:
        return None

    # DuckDB DATE / TIMESTAMP objects.
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    # Numeric normalization.
    if isinstance(value, float):
        if value != value:
            return None

        if value.is_integer():
            return int(value)

        return round(value, 6)

    if isinstance(value, int):
        return value

    # DuckDB decimal-like values.
    if hasattr(value, "as_integer_ratio"):
        try:
            numeric = float(value)

            if numeric.is_integer():
                return int(numeric)

            return round(numeric, 6)

        except Exception:
            pass

    return value
