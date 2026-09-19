from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import duckdb

from app.config import Settings
from app.errors import AnalysisError, NotFoundError
from app.gemini import GeminiService
from app.models import AnalysisResult, QueryPlan
from app.storage import Storage

log = logging.getLogger(__name__)


SQL_SYSTEM = """
You generate read-only DuckDB SQL for an uploaded CSV or Excel dataset.

The dataset is exposed to you as a table named `dataset`.

You MUST use only:
- the table `dataset`
- columns present in the supplied schema/profile
- standard DuckDB SQL functions

Your SQL must answer the user's question directly.

Rules:
1. Generate exactly one SQL SELECT statement.
2. Do not generate INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, COPY,
   ATTACH, DETACH, INSTALL, LOAD, CALL, PRAGMA, or any other statement
   that changes state or accesses external resources.
3. Do not invent columns, values, entities, years, or metrics.
4. Use the exact column names from the supplied profile.
5. If aggregation is requested, perform the calculation in SQL.
6. If ranking is requested, use ORDER BY and LIMIT.
7. If grouping is requested, use GROUP BY.
8. If a percentage change or year-over-year calculation is requested,
   perform the arithmetic in SQL.
9. Return a small result whenever possible.
10. Do not use Markdown fences.
11. Return JSON with exactly this structure:

{
  "sql": "SELECT ...",
  "explanation": "short explanation of what the query calculates"
}

The SQL must be executable by DuckDB.
"""


def run_analysis(
    plan: QueryPlan,
    store: Storage,
    gemini: GeminiService,
    settings: Settings | None = None,
) -> list[AnalysisResult]:
    """
    Execute structured CSV/Excel analysis using DuckDB.

    The existing SQLite database remains the application's metadata store.
    The stored csv_profile is used as the schema/context for SQL generation.
    DuckDB executes the generated SQL against the original uploaded file.
    """
    settings = settings or store.settings

    document = _select_dataset_document(plan, store)
    profile = store.parse_csv_profile(document.get("csv_profile"))

    if not profile:
        raise AnalysisError(
            f"No CSV schema profile is available for '{document['filename']}'."
        )

    question = _analysis_question(plan)

    sql_payload = _generate_sql(
        question=question,
        profile=profile,
        plan=plan,
        gemini=gemini,
    )

    sql = sql_payload["sql"]
    explanation = sql_payload.get("explanation", "").strip()

    _validate_sql(sql)

    file_path = store.document_file(document["id"])

    try:
        value, table, columns = _execute_sql(
            sql=sql,
            file_path=file_path,
            file_type=document["file_type"],
        )
    except Exception as exc:
        log.exception(
            "duckdb_analysis_failed document_id=%s sql=%s",
            document["id"],
            sql,
        )
        raise AnalysisError(
            "The dataset could not be analyzed with the generated SQL."
        ) from exc

    rows_used = len(table) if table else None

    result = AnalysisResult(
        operation=_operation_name(plan),
        value=value,
        table=table,
        inputs={
            "question": question,
            "sql": sql,
            "explanation": explanation,
            "document_id": document["id"],
            "filename": document["filename"],
        },
        formula=sql,
        source_file=document["filename"],
        rows_used=rows_used,
        columns_used=columns,
    )

    log.info(
        "duckdb_analysis_complete document=%s operation=%s",
        document["filename"],
        result.operation,
    )

    return [result]


def _select_dataset_document(
    plan: QueryPlan,
    store: Storage,
) -> dict[str, Any]:
    documents = store.list_documents(ready_only=True)

    datasets = [
        doc
        for doc in documents
        if doc["file_type"] in {"csv", "excel"}
    ]

    if not datasets:
        raise NotFoundError("No ready CSV or Excel dataset was found.")

    if plan.document_ids:
        for document_id in plan.document_ids:
            for doc in datasets:
                if doc["id"] == document_id:
                    return doc

    if plan.document_hints:
        lowered_hints = [hint.lower().strip() for hint in plan.document_hints]

        for doc in datasets:
            filename = doc["filename"].lower()

            if any(
                hint in filename or filename in hint
                for hint in lowered_hints
            ):
                return doc

    return datasets[0]


def _analysis_question(plan: QueryPlan) -> str:
    question = getattr(plan, "resolved_question", None)

    if isinstance(question, str) and question.strip():
        return question.strip()

    retrieval_query = getattr(plan, "retrieval_query", None)

    if isinstance(retrieval_query, str) and retrieval_query.strip():
        return retrieval_query.strip()

    parts: list[str] = []

    if plan.intent:
        parts.append(plan.intent.value)

    parts.extend(plan.entities)
    parts.extend(plan.metrics)
    parts.extend(str(year) for year in plan.years)

    for item in plan.filters:
        if isinstance(item, dict):
            column = item.get("column") or item.get("field")
            value = item.get("value")

            if column:
                parts.append(str(column))

            if value is not None:
                parts.append(str(value))

    question = " ".join(part for part in parts if part)

    if not question:
        raise AnalysisError("The dataset analysis question could not be determined.")

    return question


def _generate_sql(
    *,
    question: str,
    profile: dict[str, Any],
    plan: QueryPlan,
    gemini: GeminiService,
) -> dict[str, str]:
    schema_payload = {
        "columns": profile.get("columns", []),
        "dtypes": profile.get("dtypes", {}),
        "row_count": profile.get("row_count"),
        "null_counts": profile.get("null_counts", {}),
        "numeric_columns": profile.get("numeric_columns", []),
        "year_columns": profile.get("year_columns", []),
        "entity_columns": profile.get("entity_columns", []),
        "categorical_columns": profile.get("categorical_columns", []),
    }

    planner_context = {
        "intent": plan.intent.value if plan.intent else "",
        "entities": plan.entities,
        "metrics": plan.metrics,
        "years": plan.years,
        "filters": plan.filters,
        "operations": [
            operation.model_dump(exclude_none=True)
            for operation in plan.operations
        ],
    }

    user_payload = {
        "question": question,
        "schema": schema_payload,
        "planner_context": planner_context,
    }

    try:
        response = gemini.generate_json(
            SQL_SYSTEM,
            json.dumps(user_payload, ensure_ascii=False),
        )
    except Exception as exc:
        log.exception("sql_generation_failed")
        raise AnalysisError(
            "The dataset query could not be generated."
        ) from exc

    if not isinstance(response, dict):
        raise AnalysisError("The SQL generator returned an invalid response.")

    sql = response.get("sql")

    if not isinstance(sql, str) or not sql.strip():
        raise AnalysisError("The SQL generator did not return a query.")

    explanation = response.get("explanation", "")

    if not isinstance(explanation, str):
        explanation = str(explanation)

    return {
        "sql": sql.strip(),
        "explanation": explanation,
    }


def _validate_sql(sql: str) -> None:
    """
    Perform conservative validation before DuckDB execution.

    The LLM is only allowed to produce a single read-only SELECT query.
    """
    normalized = sql.strip()

    if not normalized:
        raise AnalysisError("Generated SQL is empty.")

    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()

    if ";" in normalized:
        raise AnalysisError("Generated SQL must contain only one statement.")

    if not re.match(r"(?is)^select\b", normalized):
        raise AnalysisError("Generated SQL must be a SELECT statement.")

    blocked = {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "replace",
        "truncate",
        "copy",
        "attach",
        "detach",
        "install",
        "load",
        "call",
        "pragma",
        "export",
        "import",
        "vacuum",
    }

    tokens = {
        token.lower()
        for token in re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", normalized)
    }

    dangerous = sorted(blocked.intersection(tokens))

    if dangerous:
        raise AnalysisError(
            "Generated SQL contains a prohibited operation."
        )

    if re.search(
        r"(?is)\b(read_csv|read_csv_auto|read_json|read_parquet|glob)\s*\(",
        normalized,
    ):
        raise AnalysisError(
            "Generated SQL cannot access files directly."
        )


def _execute_sql(
    *,
    sql: str,
    file_path: Path,
    file_type: str,
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    connection = duckdb.connect(database=":memory:")

    try:
        _register_dataset(
            connection=connection,
            file_path=file_path,
            file_type=file_type,
        )

        cursor = connection.execute(sql)

        columns = [
            description[0]
            for description in cursor.description or []
        ]

        rows = cursor.fetchall()

        table = [
            {
                column: _clean_value(value)
                for column, value in zip(columns, row, strict=False)
            }
            for row in rows
        ]

        value = _extract_scalar_value(
            columns=columns,
            rows=rows,
        )

        return value, table, columns

    finally:
        connection.close()


def _register_dataset(
    *,
    connection: duckdb.DuckDBPyConnection,
    file_path: Path,
    file_type: str,
) -> None:
    escaped_path = str(file_path).replace("'", "''")

    if file_type == "csv":
        connection.execute(
            f"""
            CREATE VIEW dataset AS
            SELECT *
            FROM read_csv_auto('{escaped_path}', header=true)
            """
        )
        return

    if file_type == "excel":
        connection.execute(
            f"""
            CREATE VIEW dataset AS
            SELECT *
            FROM read_xlsx('{escaped_path}', header=true)
            """
        )
        return

    raise AnalysisError(
        f"Unsupported dataset type: {file_type}."
    )


def _extract_scalar_value(
    *,
    columns: list[str],
    rows: list[tuple[Any, ...]],
) -> Any:
    if not rows or not columns:
        return None

    if len(rows) == 1 and len(columns) == 1:
        return _clean_value(rows[0][0])

    return None


def _clean_value(value: Any) -> Any:
    if value is None:
        return None

    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass

    if isinstance(value, float):
        if value != value:
            return None

        if value.is_integer():
            return int(value)

    return value


def _operation_name(plan: QueryPlan) -> str:
    for operation in plan.operations:
        if operation.op != "retrieve":
            return operation.op

    intent = plan.intent.value if plan.intent else "csv_analysis"

    mapping = {
        "sum": "sum",
        "average": "average",
        "minimum": "min",
        "maximum": "max",
        "ranking": "rank",
        "sorting": "sort",
        "filtering": "filter",
        "percentage_change": "percentage_change",
        "year_over_year_comparison": "yoy",
        "entity_comparison": "compare",
        "csv_aggregation": "aggregation",
        "numerical_lookup": "lookup",
    }

    return mapping.get(intent, "sql_analysis")

