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

_READ_ONLY_PREFIXES = {
    "select",
    "with",
    "from",
}

_BLOCKED_SQL_PATTERNS = (
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\bdrop\b",
    r"\balter\b",
    r"\bcreate\b",
    r"\breplace\b",
    r"\btruncate\b",
    r"\bcopy\b",
    r"\battach\b",
    r"\bdetach\b",
    r"\binstall\b",
    r"\bload\b",
    r"\bpragma\b",
    r"\bcall\b",
    r"\bexport\b",
    r"\bimport\b",
)

_SQL_GENERATION_SYSTEM = """
You generate read-only DuckDB SQL for an analytics application.

The SQL will be executed against a DuckDB view named `dataset`.

Rules:
1. Generate ONLY one read-only SQL statement.
2. The statement must begin with SELECT or WITH.
3. Never use INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, REPLACE,
   TRUNCATE, COPY, ATTACH, DETACH, INSTALL, LOAD, PRAGMA, CALL,
   EXPORT, or IMPORT.
4. Never access the filesystem directly.
5. Never create or modify tables, views, files, or databases.
6. Use only columns that exist in the supplied schema.
7. Preserve exact column names by double-quoting identifiers when necessary.
8. Return a useful result for the user's question.
9. For aggregate questions, return the calculated value with a clear alias.
10. For ranking/sorting questions, return the requested rows.
11. For comparisons, return the compared entities and their values.
12. For percentage/YoY questions, calculate the percentage in SQL.
13. Do not invent columns or values.
14. Do not explain the SQL outside the JSON response.

Return JSON in exactly this shape:
{
  "sql": "SELECT ...",
  "operation": "sum|average|min|max|count|sort|rank|filter|groupby|percentage_change|yoy|compare",
  "description": "short description"
}
"""


def needs_analysis(plan: QueryPlan) -> bool:
    return any(
        op.op in ANALYSIS_OPS and op.op != "retrieve"
        for op in plan.operations
    )


def run_analysis(
    plan: QueryPlan,
    store: Storage,
    gemini: GeminiService,
) -> list[AnalysisResult]:
    """
    Execute structured CSV/Excel analysis using DuckDB.

    The original CSV/Excel file remains in data/uploads.
    DuckDB reads that file directly and does not modify SQLite,
    Chroma, or the uploaded file.
    """

    results: list[AnalysisResult] = []

    dataset_file: Path | None = None
    source_file = ""

    for op in plan.operations:
        if op.op == "retrieve":
            continue

        if op.op == "load_csv":
            dataset_file, source_file, profile = _resolve_dataset(
                store=store,
                plan=plan,
                op=op,
            )

            results.append(
                AnalysisResult(
                    operation="load_csv",
                    source_file=source_file,
                    rows_used=int(profile.get("row_count", 0)),
                    columns_used=list(profile.get("columns", [])),
                    inputs={
                        "filename": source_file,
                        "file_type": profile.get("file_type"),
                    },
                )
            )
            continue

        if dataset_file is None:
            dataset_file, source_file, _ = _resolve_dataset(
                store=store,
                plan=plan,
                op=PlanOp(
                    op="load_csv",
                    filename_hint=(
                        plan.document_hints[0]
                        if plan.document_hints
                        else None
                    ),
                ),
            )

        result = _run_duckdb_operation(
            dataset_file=dataset_file,
            source_file=source_file,
            op=op,
            plan=plan,
            store=store,
            gemini=gemini,
        )

        results.append(result)

        log.info(
            "duckdb_analysis op=%s file=%s",
            op.op,
            source_file,
        )

    return results


def _resolve_dataset(
    *,
    store: Storage,
    plan: QueryPlan,
    op: PlanOp,
) -> tuple[Path, str, dict[str, Any]]:
    docs = [
        d
        for d in store.list_documents(ready_only=True)
        if d["file_type"] in {"csv", "excel"}
    ]

    if not docs:
        raise AnalysisError(
            "No CSV or Excel dataset is available for this calculation."
        )

    hint = (
        op.filename_hint
        or (
            plan.document_hints[0]
            if plan.document_hints
            else ""
        )
    ).strip().lower()

    chosen = None

    if hint:
        for doc in docs:
            if hint in doc["filename"].lower():
                chosen = doc
                break

    if chosen is None and plan.document_ids:
        for doc in docs:
            if doc["id"] in plan.document_ids:
                chosen = doc
                break

    if chosen is None:
        if len(docs) == 1:
            chosen = docs[0]
        else:
            raise AnalysisError(
                "Multiple CSV or Excel datasets are available. "
                "The question must identify which dataset to analyze."
            )

    try:
        path = store.document_file(chosen["id"])
    except NotFoundError:
        raise
    except Exception as exc:
        raise AnalysisError(
            "The source dataset could not be located."
        ) from exc

    if not path.exists():
        raise AnalysisError(
            "The source dataset file no longer exists."
        )

    profile = store.parse_csv_profile(
        chosen.get("csv_profile")
    ) or {}

    profile["file_type"] = chosen["file_type"]

    return (
        path,
        chosen["filename"],
        profile,
    )


def _run_duckdb_operation(
    *,
    dataset_file: Path,
    source_file: str,
    op: PlanOp,
    plan: QueryPlan,
    store: Storage,
    gemini: GeminiService,
) -> AnalysisResult:
    profile = _profile_for_document(
        store=store,
        plan=plan,
        source_file=source_file,
    )

    sql_payload = _generate_sql(
        gemini=gemini,
        question=plan.question,
        operation=op,
        plan=plan,
        profile=profile,
    )

    sql = _validate_sql(
        sql_payload.get("sql")
    )

    description = str(
        sql_payload.get("description")
        or ""
    ).strip()

    log.info(
        "duckdb_sql_generated operation=%s sql=%s",
        op.op,
        sql,
    )

    try:
        with duckdb.connect(database=":memory:") as conn:
            _create_dataset_view(
                conn=conn,
                dataset_file=dataset_file,
            )

            cursor = conn.execute(sql)

            rows = cursor.fetchall()
            columns = [
                str(description[0])
                for description in cursor.description
            ]

    except Exception as exc:
        log.exception(
            "duckdb_analysis_failed operation=%s file=%s",
            op.op,
            source_file,
        )
        raise AnalysisError(
            "The requested dataset analysis could not be completed."
        ) from exc

    table = [
        {
            column: _normalize_value(value)
            for column, value in zip(columns, row)
        }
        for row in rows
    ]

    return _build_analysis_result(
        op=op,
        source_file=source_file,
        sql=sql,
        description=description,
        table=table,
        columns=columns,
        row_count=len(rows),
    )


def _create_dataset_view(
    *,
    conn: duckdb.DuckDBPyConnection,
    dataset_file: Path,
) -> None:
    suffix = dataset_file.suffix.lower()

    escaped = str(dataset_file).replace("'", "''")

    if suffix == ".csv":
        sql = (
            "CREATE VIEW dataset AS "
            f"SELECT * FROM read_csv_auto('{escaped}', "
            "header=true, auto_detect=true)"
        )
        conn.execute(sql)
        return

    if suffix in {".xlsx", ".xls"}:
        try:
            conn.execute(
                "LOAD excel"
            )
        except Exception as exc:
            raise AnalysisError(
                "DuckDB Excel support is not available. "
                "Install or enable the DuckDB Excel extension."
            ) from exc

        conn.execute(
            "CREATE VIEW dataset AS "
            f"SELECT * FROM read_xlsx('{escaped}')"
        )
        return

    raise AnalysisError(
        f"Unsupported dataset format '{suffix}'."
    )


def _generate_sql(
    *,
    gemini: GeminiService,
    question: str,
    operation: PlanOp,
    plan: QueryPlan,
    profile: dict[str, Any],
) -> dict[str, Any]:
    schema = {
        "columns": profile.get("columns", []),
        "dtypes": profile.get("dtypes", {}),
        "numeric_columns": profile.get(
            "numeric_columns",
            [],
        ),
        "year_columns": profile.get(
            "year_columns",
            [],
        ),
        "entity_columns": profile.get(
            "entity_columns",
            [],
        ),
        "categorical_columns": profile.get(
            "categorical_columns",
            [],
        ),
        "row_count": profile.get(
            "row_count",
            0,
        ),
    }

    payload = {
        "question": question,
        "requested_operation": operation.model_dump(
            exclude_none=True
        ),
        "plan": {
            "intent": getattr(
                plan.intent,
                "value",
                str(plan.intent),
            ),
            "metrics": plan.metrics,
            "entities": plan.entities,
            "years": plan.years,
            "filters": plan.filters,
            "document_hints": plan.document_hints,
            "document_ids": plan.document_ids,
        },
        "schema": schema,
        "dataset_view": "dataset",
    }

    try:
        response = gemini.generate_json(
            system_prompt=_SQL_GENERATION_SYSTEM,
            user_prompt=json.dumps(
                payload,
                ensure_ascii=False,
                default=str,
            ),
        )
    except Exception as exc:
        raise AnalysisError(
            "The analysis query could not be generated."
        ) from exc

    if not isinstance(response, dict):
        raise AnalysisError(
            "The analysis query generator returned an invalid response."
        )

    return response


def _validate_sql(sql: Any) -> str:
    if not isinstance(sql, str):
        raise AnalysisError(
            "The generated analysis query is invalid."
        )

    sql = sql.strip()

    if not sql:
        raise AnalysisError(
            "The generated analysis query is empty."
        )

    # Remove a surrounding markdown fence if Gemini adds one.
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
        ).strip()

    if ";" in sql.rstrip(";"):
        raise AnalysisError(
            "Only one SQL statement is allowed."
        )

    sql = sql.rstrip(";").strip()

    normalized = re.sub(
        r"\s+",
        " ",
        sql.lower(),
    ).strip()

    if not any(
        normalized.startswith(prefix)
        for prefix in _READ_ONLY_PREFIXES
    ):
        raise AnalysisError(
            "Only read-only SELECT queries are allowed."
        )

    for pattern in _BLOCKED_SQL_PATTERNS:
        if re.search(
            pattern,
            normalized,
            flags=re.IGNORECASE,
        ):
            raise AnalysisError(
                "The generated SQL contains a prohibited operation."
            )

    if "dataset" not in normalized:
        raise AnalysisError(
            "The generated SQL must query the dataset view."
        )

    return sql


def _profile_for_document(
    *,
    store: Storage,
    plan: QueryPlan,
    source_file: str,
) -> dict[str, Any]:
    for document in store.list_documents(
        ready_only=True
    ):
        if document["filename"] == source_file:
            profile = store.parse_csv_profile(
                document.get("csv_profile")
            )

            if profile:
                return profile

    raise AnalysisError(
        "The dataset schema could not be found."
    )


def _build_analysis_result(
    *,
    op: PlanOp,
    source_file: str,
    sql: str,
    description: str,
    table: list[dict[str, Any]],
    columns: list[str],
    row_count: int,
) -> AnalysisResult:
    operation = op.op

    value: Any = None

    if operation in {
        "sum",
        "average",
        "min",
        "max",
        "count",
        "percentage_change",
        "yoy",
    }:
        value = _extract_scalar_value(
            table
        )

    elif operation == "compare":
        value = _extract_compare_value(
            table
        )

    return AnalysisResult(
        operation=operation,
        value=value,
        table=table or None,
        formula=sql,
        source_file=source_file,
        rows_used=row_count,
        columns_used=columns,
        inputs={
            "description": description,
        },
    )


def _extract_scalar_value(
    table: list[dict[str, Any]],
) -> float | int | None:
    if not table:
        return None

    first = table[0]

    if len(first) == 1:
        value = next(iter(first.values()))
        return _normalize_number(value)

    preferred = (
        "value",
        "result",
        "total",
        "average",
        "avg",
        "minimum",
        "maximum",
        "count",
        "percentage_change",
        "yoy",
    )

    lowered = {
        str(key).lower(): value
        for key, value in first.items()
    }

    for key in preferred:
        if key in lowered:
            return _normalize_number(
                lowered[key]
            )

    numeric_values = [
        value
        for value in first.values()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
    ]

    if len(numeric_values) == 1:
        return _normalize_number(
            numeric_values[0]
        )

    return None


def _extract_compare_value(
    table: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not table:
        return None

    if len(table) == 1:
        row = table[0]

        if len(row) == 1:
            return None

    result: dict[str, Any] = {}

    for row in table:
        if len(row) < 2:
            continue

        items = list(row.items())

        entity = items[0][1]
        value = items[-1][1]

        if entity is not None:
            result[str(entity)] = _normalize_value(
                value
            )

    return result or None


def _normalize_value(
    value: Any,
) -> Any:
    if value is None:
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return _normalize_number(value)

    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    return value


def _normalize_number(
    value: Any,
) -> Any:
    if isinstance(value, bool):
        return value

    if isinstance(value, float):
        if value != value:
            return None

        if value.is_integer():
            return int(value)

        return round(value, 6)

    return value
