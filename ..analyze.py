from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import duckdb

from app.config import Settings
from app.errors import AnalysisError, NotFoundError
from app.models import AnalysisResult, QueryPlan
from app.storage import Storage


log = logging.getLogger(__name__)


SQL_SYSTEM = """
You are the SQL analysis engine for an intelligent document analysis system.

Your job is to convert a user's natural-language question into ONE
read-only DuckDB SQL query.

The query will run against a DuckDB relation named:

    dataset

The relation contains the actual contents of a user-provided CSV file.

You MUST answer the user's question by querying dataset.

IMPORTANT RULES:

1. Return ONLY valid JSON.
2. The JSON must have exactly this general structure:

   {
     "sql": "SELECT ...",
     "operation": "..."
   }

3. Generate exactly ONE SQL statement.
4. The statement must be read-only.
5. The query must use ONLY the relation `dataset`.
6. Do NOT read files directly.
7. Do NOT use:
   - read_csv
   - read_csv_auto
   - read_parquet
   - read_json
   - read_xlsx
   - glob
   - httpfs
   - http_get
   - attach
   - install
   - load
   - copy
8. Do NOT create, replace, alter, drop, insert, update, delete, merge,
   export, or write anything.
9. SELECT and WITH queries are allowed.
10. Use only columns that actually exist in the supplied schema.
11. Do not invent columns.
12. Preserve the meaning of the user's question.
13. Perform arithmetic and aggregation in SQL rather than asking the
    language model to calculate from raw rows.
14. For totals, use SUM.
15. For averages, use AVG.
16. For minimum and maximum, use MIN and MAX.
17. For counts, use COUNT.
18. For rankings, use ORDER BY and LIMIT where appropriate.
19. For percentage calculations, calculate the percentage in SQL.
20. For year-over-year calculations, calculate the values and the change
    in SQL.
21. If the user asks for a comparison, return the comparison directly.
22. If grouping is required, use GROUP BY.
23. If filtering is required, use WHERE.
24. If the question asks for the top N or bottom N, respect N.
25. If the dataset contains dates, use DuckDB date functions where useful.
26. Never assume a column exists if it is not present in the schema.
27. String comparisons should be reasonably tolerant of capitalization.
28. When filtering textual values, prefer LOWER(column) = LOWER('value')
    when exact matching is intended.
29. When the user asks for a calculation involving a percentage, protect
    against division by zero with NULLIF.
30. Do not put markdown fences around SQL.
31. Do not explain the SQL outside the JSON response.

The SQL should return a result that is directly useful for answering the
user's question.
"""


FORBIDDEN_SQL_PATTERNS = (
    r"\battach\b",
    r"\bcopy\b",
    r"\bcreate\b",
    r"\breplace\b",
    r"\balter\b",
    r"\bdrop\b",
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\bmerge\b",
    r"\bexport\b",
    r"\binstall\b",
    r"\bload\b",
    r"\bpragma\b",
    r"\bvacuum\b",
    r"\bcall\b",
    r"\bread_csv\b",
    r"\bread_csv_auto\b",
    r"\bread_parquet\b",
    r"\bread_json\b",
    r"\bread_xlsx\b",
    r"\bglob\b",
    r"\bhttpfs\b",
    r"\bhttp_get\b",
)

ALLOWED_START_PATTERN = re.compile(
    r"^\s*(select|with)\b",
    re.IGNORECASE,
)

DATASET_PATTERN = re.compile(
    r"\bdataset\b",
    re.IGNORECASE,
)


def needs_analysis(plan: QueryPlan) -> bool:
    """
    Return True when the query requires structured-data analysis.

    `load_csv` alone means that structured data is involved, but a load
    operation by itself is not an analysis request. A real analysis
    operation must also be present.
    """

    analysis_operations = {
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

    return any(
        operation.op in analysis_operations
        for operation in plan.operations
    )


def run_analysis(
    plan: QueryPlan,
    question: str,
    store: Storage,
    gemini: Any,
    settings: Settings,
) -> AnalysisResult:
    """
    Execute LLM-generated read-only SQL against a structured document.

    The LLM is responsible for understanding the question and generating
    SQL. DuckDB is responsible for reading and calculating from the actual
    CSV data.

    No question-specific analytical logic is implemented in Python here.
    """

    document = _choose_document(
        plan=plan,
        store=store,
    )

    document_id = document["id"]
    filename = document["filename"]
    file_type = document["file_type"]

    if file_type.lower() not in {
        "csv",
        "xlsx",
        "xls",
    }:
        raise AnalysisError(
            "Structured analysis requires a CSV or Excel document."
        )

    file_path = store.document_file(document_id)

    if file_path is None:
        raise NotFoundError(
            f"Source file for document {filename!r} was not found."
        )

    file_path = Path(file_path)

    if not file_path.exists():
        raise NotFoundError(
            f"Source file for document {filename!r} was not found."
        )

    profile = store.parse_csv_profile(
        document.get("csv_profile")
    )

    schema = _build_schema_description(
        profile=profile,
        file_path=file_path,
    )

    plan_description = _build_plan_description(
        plan=plan,
    )

    prompt = _build_sql_prompt(
        question=question,
        filename=filename,
        schema=schema,
        plan_description=plan_description,
    )

    generated = gemini.generate_json(
        prompt,
        system_instruction=SQL_SYSTEM,
        temperature=0.0,
    )

    sql = generated.get("sql")

    if not isinstance(sql, str) or not sql.strip():
        raise AnalysisError(
            "Gemini did not return a SQL query."
        )

    sql = _clean_sql(sql)

    _validate_sql(sql)

    operation = generated.get(
        "operation",
        "query",
    )

    if not isinstance(operation, str):
        operation = "query"

    log.info(
        "executing_duckdb_analysis document=%s operation=%s",
        filename,
        operation,
    )

    try:
        return _execute_query(
            sql=sql,
            operation=operation,
            file_path=file_path,
            file_type=file_type,
            filename=filename,
        )
    except AnalysisError:
        raise
    except Exception as exc:
        log.exception(
            "duckdb_analysis_failed document=%s",
            filename,
        )
        raise AnalysisError(
            "The generated query could not be executed against the "
            "document."
        ) from exc


def _choose_document(
    plan: QueryPlan,
    store: Storage,
) -> dict[str, Any]:
    """
    Resolve the structured document to analyze.

    Prefer an explicitly selected document. Otherwise use filename hints.
    If exactly one structured document is available, use it.

    The planner remains responsible for deciding which document is relevant;
    this function only resolves that decision against storage.
    """

    documents = store.list_documents()

    ready = [
        document
        for document in documents
        if document.get("status") == "ready"
    ]

    structured = [
        document
        for document in ready
        if str(document.get("file_type", "")).lower()
        in {"csv", "xlsx", "xls"}
    ]

    if not structured:
        raise NotFoundError(
            "No ready CSV or Excel document is available for analysis."
        )

    if plan.document_ids:
        for document_id in plan.document_ids:
            for document in structured:
                if document.get("id") == document_id:
                    return document

    if plan.document_hints:
        lowered_hints = [
            hint.lower()
            for hint in plan.document_hints
        ]

        for document in structured:
            filename = str(
                document.get("filename", "")
            ).lower()

            if any(
                hint in filename
                for hint in lowered_hints
            ):
                return document

    if len(structured) == 1:
        return structured[0]

    raise AnalysisError(
        "More than one CSV or Excel document is available. "
        "The question must identify which document should be analyzed."
    )


def _build_schema_description(
    profile: dict[str, Any],
    file_path: Path,
) -> str:
    """
    Build a compact schema description for Gemini.

    The model receives the actual columns and inferred types rather than
    having to guess them.
    """

    columns = profile.get("columns")

    if not isinstance(columns, list):
        columns = []

    dtypes = profile.get("dtypes")

    if not isinstance(dtypes, dict):
        dtypes = {}

    row_count = profile.get("row_count")

    if row_count is None:
        row_count = "unknown"

    lines: list[str] = [
        f"File: {file_path.name}",
        f"Rows: {row_count}",
        "",
        "Columns:",
    ]

    for column in columns:
        if not isinstance(column, str):
            continue

        dtype = dtypes.get(
            column,
            "unknown",
        )

        lines.append(
            f"- {column}: {dtype}"
        )

    if not columns:
        lines.append(
            "No profile columns were available. "
            "DuckDB schema inspection will be used."
        )

    return "\n".join(lines)


def _build_plan_description(
    plan: QueryPlan,
) -> str:
    """
    Give Gemini the planner's interpretation without making Python
    responsible for answering the question.

    This is supporting context only. The SQL model still sees the original
    user question and the actual dataset schema.
    """

    data = {
        "intent": plan.intent.value,
        "entities": plan.entities,
        "metrics": plan.metrics,
        "years": plan.years,
        "filters": plan.filters,
        "document_ids": plan.document_ids,
        "document_hints": plan.document_hints,
        "operations": [
            operation.model_dump(
                by_alias=True,
            )
            for operation in plan.operations
        ],
        "resolved_question": plan.resolved_question,
        "is_follow_up": plan.is_follow_up,
    }

    return json.dumps(
        data,
        ensure_ascii=False,
        default=str,
    )


def _build_sql_prompt(
    question: str,
    filename: str,
    schema: str,
    plan_description: str,
) -> str:
    return f"""
User question:

{question}

Structured document selected for analysis:

{filename}

Actual dataset schema:

{schema}

Planner interpretation:

{plan_description}

Generate the single DuckDB SQL query needed to answer the user's question.

Remember:

- The only available relation is `dataset`.
- Use only columns from the actual schema.
- Query the data rather than guessing values.
- Perform all required calculations in SQL.
- Return one JSON object containing `sql` and `operation`.
"""


def _clean_sql(sql: str) -> str:
    """
    Normalize SQL returned by Gemini without changing its meaning.
    """

    value = sql.strip()

    if value.startswith("```"):
        value = re.sub(
            r"^```(?:sql)?\s*",
            "",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(
            r"\s*```$",
            "",
            value,
        )

    value = value.strip()

    if value.endswith(";"):
        value = value[:-1].rstrip()

    return value


def _validate_sql(sql: str) -> None:
    """
    Validate generated SQL before executing it.

    The query must be a single SELECT/WITH statement and must operate on
    the registered `dataset` relation.
    """

    if not sql:
        raise AnalysisError(
            "Gemini generated an empty SQL query."
        )

    if ";" in sql:
        raise AnalysisError(
            "Generated SQL contains multiple statements."
        )

    if "--" in sql or "/*" in sql or "*/" in sql:
        raise AnalysisError(
            "Generated SQL contains comments and was rejected."
        )

    if not ALLOWED_START_PATTERN.search(sql):
        raise AnalysisError(
            "Generated SQL must be a SELECT or WITH query."
        )

    for pattern in FORBIDDEN_SQL_PATTERNS:
        if re.search(
            pattern,
            sql,
            flags=re.IGNORECASE,
        ):
            raise AnalysisError(
                "Generated SQL contains a forbidden operation."
            )

    if not DATASET_PATTERN.search(sql):
        raise AnalysisError(
            "Generated SQL does not reference the dataset."
        )

    _validate_dataset_references(sql)


def _validate_dataset_references(sql: str) -> None:
    """
    Prevent generated SQL from directly accessing arbitrary relations.

    We intentionally allow CTE names and aliases, but reject common
    multi-table constructs that could bypass the registered dataset.
    """

    lowered = sql.lower()

    if re.search(
        r"\bfrom\s+(?!dataset\b)",
        lowered,
    ):
        raise AnalysisError(
            "Generated SQL attempted to read a relation other than dataset."
        )

    if re.search(
        r"\bjoin\s+(?!dataset\b)",
        lowered,
    ):
        raise AnalysisError(
            "Generated SQL attempted to join a relation other than dataset."
        )

    if re.search(
        r"\bunion\s+(?:all\s+)?select\b",
        lowered,
    ):
        # UNION is allowed only when each branch remains based on dataset.
        relation_names = re.findall(
            r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
            lowered,
        )

        invalid = [
            name
            for name in relation_names
            if name != "dataset"
        ]

        if invalid:
            raise AnalysisError(
                "Generated SQL references an unauthorized relation."
            )


def _execute_query(
    sql: str,
    operation: str,
    file_path: Path,
    file_type: str,
    filename: str,
) -> AnalysisResult:
    """
    Register the actual file as `dataset` and execute the generated query.

    DuckDB runs entirely in memory. The generated SQL never receives direct
    filesystem access.
    """

    connection = duckdb.connect(
        database=":memory:",
    )

    try:
        _register_dataset(
            connection=connection,
            file_path=file_path,
            file_type=file_type,
        )

        cursor = connection.execute(sql)

        columns = [
            description[0]
            for description in cursor.description
        ]

        rows = cursor.fetchall()

        table = [
            {
                column: _normalize_value(value),
                for column, value in zip(
                    columns,
                    row,
                )
            }
            for row in rows
        ]

        value = _extract_scalar_value(
            table=table,
        )

        return AnalysisResult(
            operation=operation,
            value=value,
            table=table,
            inputs={
                "question_file": filename,
                "file_path": file_path.name,
                "sql": sql,
            },
            formula=None,
            source_file=filename,
            rows_used=_count_dataset_rows(
                connection,
            ),
            columns_used=columns,
        )

    except Exception as exc:
        raise AnalysisError(
            f"DuckDB could not execute the generated query: {exc}"
        ) from exc

    finally:
        connection.close()


def _register_dataset(
    connection: duckdb.DuckDBPyConnection,
    file_path: Path,
    file_type: str,
) -> None:
    """
    Register the actual source file as the relation `dataset`.

    CSV is the primary structured-data path.

    Excel support is retained when the DuckDB Excel extension is already
    available locally. We do not automatically INSTALL extensions because
    corporate environments may block network access.
    """

    normalized_type = file_type.lower()

    escaped_path = _duckdb_string_literal(
        file_path,
    )

    if normalized_type == "csv":
        connection.execute(
            f"""
            CREATE VIEW dataset AS
            SELECT *
            FROM read_csv_auto(
                {escaped_path},
                header = true,
                sample_size = -1
            )
            """
        )
        return

    if normalized_type in {"xlsx", "xls"}:
        try:
            connection.execute(
                "LOAD excel"
            )
        except Exception as exc:
            raise AnalysisError(
                "Excel analysis requires DuckDB's Excel extension. "
                "The extension is not available in this environment."
            ) from exc

        connection.execute(
            f"""
            CREATE VIEW dataset AS
            SELECT *
            FROM read_xlsx(
                {escaped_path},
                header = true
            )
            """
        )
        return

    raise AnalysisError(
        f"Unsupported structured document type: {file_type}"
    )


def _duckdb_string_literal(path: Path) -> str:
    """
    Safely represent a local filesystem path as a DuckDB SQL string literal.
    """

    value = str(
        path.resolve()
    )

    return "'" + value.replace(
        "'",
        "''",
    ) + "'"


def _extract_scalar_value(
    table: list[dict[str, Any]],
) -> Any:
    """
    Convert a one-cell/one-row result into a convenient scalar.

    Larger result sets remain available through `table`.
    """

    if len(table) != 1:
        return None

    row = table[0]

    if len(row) != 1:
        return None

    return next(
        iter(row.values())
    )


def _count_dataset_rows(
    connection: duckdb.DuckDBPyConnection,
) -> int:
    try:
        result = connection.execute(
            "SELECT COUNT(*) FROM dataset"
        ).fetchone()

        if not result:
            return 0

        return int(
            result[0]
        )
    except Exception:
        return 0


def _normalize_value(value: Any) -> Any:
    """
    Convert DuckDB values into JSON-friendly Python values.
    """

    if value is None:
        return None

    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass

    if isinstance(
        value,
        (
            str,
            int,
            float,
            bool,
        ),
    ):
        return value

    if isinstance(
        value,
        (
            list,
            tuple,
        ),
    ):
        return [
            _normalize_value(item)
            for item in value
        ]

    if isinstance(value, dict):
        return {
            str(key): _normalize_value(item)
            for key, item in value.items()
        }

    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)
