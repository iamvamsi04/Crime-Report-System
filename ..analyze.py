from __future__ import annotations

import json
import logging
from typing import Any

from app.dataset import LoadedDataset, load_datasets, schemas_for_prompt
from app.errors import AnalysisError, GeminiError
from app.gemini import GeminiService
from app.models import (
    AnalysisResult,
    ExecutionMode,
    GeneratedQuery,
    QueryPlan,
    StructuredResult,
)
from app.sql_executor import execute_structured_query
from app.storage import Storage

log = logging.getLogger(__name__)


DEFAULT_MAX_REPAIR_ATTEMPTS = 2
DEFAULT_MAX_RESULT_ROWS = 200


ANALYSIS_SYSTEM = """
You are the structured-data analysis engine for a document question-answering
system.

Your task is to translate the user's analytical question into ONE read-only
DuckDB SQL query over the datasets provided to you.

You are not answering the user directly.
You are producing a query that will be executed and verified by the system.

The datasets are already loaded into DuckDB as in-memory tables. You may ONLY
query the table names provided in DATASETS.

RULES

1. Return JSON only.

2. Return exactly this general structure:

{
  "sql": "SELECT ...",
  "explanation": "brief description of what the query computes",
  "tables_used": ["dataset_1"],
  "columns_used": ["Column A", "Column B"]
}

3. Generate exactly ONE read-only analytical query.

4. The query must begin with SELECT or WITH.

5. Never generate:
   INSERT
   UPDATE
   DELETE
   CREATE
   DROP
   ALTER
   COPY
   ATTACH
   DETACH
   INSTALL
   LOAD
   PRAGMA
   CALL
   EXPORT
   IMPORT
   SET
   or any other mutating/admin statement.

6. Never access files, URLs, extensions, databases, or external resources.

7. Use only the supplied in-memory table names.

8. Use the exact column names from the supplied schemas. Quote identifiers with
   double quotes whenever appropriate, especially when they contain spaces,
   punctuation, reserved words, or mixed casing.

9. Perform calculations in SQL. Do not calculate numerical answers yourself.

10. Infer the analytical operations required from the meaning of the question.
    You are NOT restricted to predefined operations.

You may use whatever read-only DuckDB SQL is necessary, including:
   filtering
   aggregation
   GROUP BY
   HAVING
   ordering
   ranking
   window functions
   joins
   CTEs
   conditional aggregation
   CASE expressions
   arithmetic
   ratios
   percentages
   percentiles
   medians
   standard deviation
   variance
   correlations
   date calculations
   string operations
   distinct counts
   deduplication analysis
   null analysis
   trend analysis
   comparisons
   multi-table analysis
   multi-step derived calculations

11. Resolve natural-language references using schema names and sample values.

Example:
If a column called "Team" has sample values ["Engineering", "Sales"], and the
user asks about Engineering, use that column even though it is not literally
called "Department".

12. Do not assume a column exists because the question mentions it. Use only
columns shown in the schema.

13. If the question requests a direct value or small answer, return only the
columns/rows needed to answer it.

14. If the question asks for a list/table, return the requested rows.

15. If the question asks for a summary or broad analysis, compute useful
descriptive results that directly support the requested summary.

16. Protect arithmetic from divide-by-zero when necessary, usually with
NULLIF(..., 0).

17. Treat NULLs deliberately.

18. For text matching, use case-insensitive comparisons when the user's wording
does not imply case sensitivity. DuckDB ILIKE may be used.

19. Do not invent missing values, rows, columns, categories, dates, or metrics.

20. When multiple datasets are provided, use only those needed to answer the
question. You may join or compare datasets when the question requires it.

21. If multiple datasets have similar schemas, do not assume rows correspond
unless there is a defensible join key or the question only requires independent
aggregates/comparisons.

22. "tables_used" must contain the actual dataset_N table names referenced by
your SQL.

23. "columns_used" should contain the important source columns used by the
analysis.

Your output will be validated and executed by another component.
"""


REPAIR_SYSTEM = """
You repair read-only DuckDB analytical SQL.

A previous SQL query was generated for a user's question, but validation or
execution failed.

You will receive:
- the original analytical question,
- available dataset schemas,
- the previous SQL,
- the validation/execution error.

Return JSON only:

{
  "sql": "corrected SELECT or WITH query",
  "explanation": "brief description of the corrected analysis",
  "tables_used": ["dataset_1"],
  "columns_used": ["Column A"]
}

RULES

- Correct the query based on the actual schemas and error.
- Generate exactly one read-only SELECT/WITH query.
- Use only supplied in-memory tables and exact schema columns.
- Never access files, URLs, extensions, or external databases.
- Never generate mutating/admin SQL.
- Do not answer the user's question yourself.
- Do not fabricate columns or data.
- Do not merely repeat the failed query unless it is actually correct.
"""


def needs_analysis(plan: QueryPlan) -> bool:
    """
    Structured and hybrid plans require execution against CSV/Excel data.
    """

    return plan.mode in {
        ExecutionMode.STRUCTURED,
        ExecutionMode.HYBRID,
    }


def run_analysis(
    *,
    question: str,
    plan: QueryPlan,
    store: Storage,
    gemini: GeminiService,
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS,
    max_result_rows: int = DEFAULT_MAX_RESULT_ROWS,
) -> list[AnalysisResult]:
    """
    Execute arbitrary structured-data analysis.

    This function intentionally contains no question-specific analytical
    implementation.

    Gemini determines the SQL required from:
        question + real dataset schemas

    DuckDB performs the computation.

    Failed generated queries can be repaired automatically using the database
    error and schema.
    """

    if not needs_analysis(plan):
        return []

    analytical_question = (
        plan.structured_question
        or question
    ).strip()

    if not analytical_question:
        analytical_question = question.strip()

    datasets = load_datasets(
        plan=plan,
        store=store,
    )

    schema_payload = schemas_for_prompt(datasets)

    generated = _generate_query(
        question=analytical_question,
        schemas=schema_payload,
        gemini=gemini,
    )

    structured_result = _execute_with_repair(
        question=analytical_question,
        generated=generated,
        datasets=datasets,
        schemas=schema_payload,
        gemini=gemini,
        max_repair_attempts=max_repair_attempts,
        max_result_rows=max_result_rows,
    )

    return [
        _to_analysis_result(
            result=structured_result,
            datasets=datasets,
        )
    ]


def _generate_query(
    *,
    question: str,
    schemas: list[dict[str, Any]],
    gemini: GeminiService,
) -> GeneratedQuery:
    user_prompt = _generation_prompt(
        question=question,
        schemas=schemas,
    )

    try:
        payload = gemini.generate_json(
            system=ANALYSIS_SYSTEM,
            user=user_prompt,
        )

        generated = GeneratedQuery.model_validate(
            payload
        )

    except AnalysisError:
        raise

    except GeminiError as exc:
        raise AnalysisError(
            "The structured analysis query could not be generated."
        ) from exc

    except Exception as exc:
        log.exception(
            "structured_query_generation_invalid"
        )

        raise AnalysisError(
            "The structured analysis query was invalid."
        ) from exc

    log.info(
        "structured_query_generated tables=%s columns=%s",
        generated.tables_used,
        generated.columns_used,
    )

    return generated


def _execute_with_repair(
    *,
    question: str,
    generated: GeneratedQuery,
    datasets: list[LoadedDataset],
    schemas: list[dict[str, Any]],
    gemini: GeminiService,
    max_repair_attempts: int,
    max_result_rows: int,
) -> StructuredResult:
    """
    Execute generated SQL and repair it when validation/binding/execution
    fails.

    Example repairable failures:
        - wrong column spelling
        - incorrect quoting
        - invalid aggregation
        - incorrect date conversion
        - ambiguous column after a join
        - unsupported SQL syntax

    Safety failures are also sent through repair, but every repaired query is
    independently revalidated by sql_executor.py before execution.
    """

    max_repair_attempts = max(
        0,
        int(max_repair_attempts),
    )

    current = generated
    last_error: AnalysisError | None = None

    for attempt in range(
        max_repair_attempts + 1
    ):
        try:
            result = execute_structured_query(
                sql=current.sql,
                datasets=datasets,
                max_result_rows=max_result_rows,
                explanation=current.explanation,
            )

            if attempt:
                log.info(
                    "structured_query_repaired attempts=%s",
                    attempt,
                )

            return result

        except AnalysisError as exc:
            last_error = exc

            log.warning(
                "structured_query_attempt_failed "
                "attempt=%s max_repairs=%s error=%s",
                attempt + 1,
                max_repair_attempts,
                str(exc),
            )

            if attempt >= max_repair_attempts:
                break

            current = _repair_query(
                question=question,
                schemas=schemas,
                previous=current,
                error=str(exc),
                gemini=gemini,
            )

    if last_error is not None:
        raise AnalysisError(
            "The requested structured analysis could not be completed "
            "against the available dataset."
        ) from last_error

    raise AnalysisError(
        "The requested structured analysis could not be completed."
    )


def _repair_query(
    *,
    question: str,
    schemas: list[dict[str, Any]],
    previous: GeneratedQuery,
    error: str,
    gemini: GeminiService,
) -> GeneratedQuery:
    user_prompt = _repair_prompt(
        question=question,
        schemas=schemas,
        previous=previous,
        error=error,
    )

    try:
        payload = gemini.generate_json(
            system=REPAIR_SYSTEM,
            user=user_prompt,
        )

        repaired = GeneratedQuery.model_validate(
            payload
        )

    except GeminiError as exc:
        raise AnalysisError(
            "The structured analysis query could not be repaired."
        ) from exc

    except Exception as exc:
        log.exception(
            "structured_query_repair_invalid"
        )

        raise AnalysisError(
            "The structured analysis query repair was invalid."
        ) from exc

    return repaired


def _generation_prompt(
    *,
    question: str,
    schemas: list[dict[str, Any]],
) -> str:
    return "\n\n".join(
        [
            "ANALYTICAL QUESTION:",
            question,
            "DATASETS:",
            json.dumps(
                schemas,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            (
                "Generate the single read-only DuckDB SQL query required "
                "to answer the analytical question."
            ),
        ]
    )


def _repair_prompt(
    *,
    question: str,
    schemas: list[dict[str, Any]],
    previous: GeneratedQuery,
    error: str,
) -> str:
    previous_payload = {
        "sql": previous.sql,
        "explanation": previous.explanation,
        "tables_used": previous.tables_used,
        "columns_used": previous.columns_used,
    }

    return "\n\n".join(
        [
            "ORIGINAL ANALYTICAL QUESTION:",
            question,
            "AVAILABLE DATASETS:",
            json.dumps(
                schemas,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            "PREVIOUS GENERATED QUERY:",
            json.dumps(
                previous_payload,
                ensure_ascii=False,
                indent=2,
            ),
            "VALIDATION OR EXECUTION ERROR:",
            error,
            (
                "Generate a corrected single read-only DuckDB query. "
                "Use only the supplied tables and columns."
            ),
        ]
    )


def _to_analysis_result(
    *,
    result: StructuredResult,
    datasets: list[LoadedDataset],
) -> AnalysisResult:
    """
    Convert verified DuckDB output into the generic evidence model consumed
    by chat/evidence layers.
    """

    value = _single_value(result)

    primary_document_id = ""
    primary_filename = ""

    if len(datasets) == 1:
        primary_document_id = datasets[0].document_id
        primary_filename = datasets[0].filename

    return AnalysisResult(
        operation="dynamic_sql",
        value=value,
        table=result.rows,
        columns=result.columns,
        formula=None,
        query=result.sql,
        filename=primary_filename,
        document_id=primary_document_id,
        source_filenames=result.source_filenames,
        source_document_ids=result.source_document_ids,
        rows_used=result.row_count,
        truncated=result.truncated,
        explanation=_analysis_explanation(result),
    )


def _single_value(
    result: StructuredResult,
) -> Any | None:
    """
    Preserve convenient scalar results for conversation context.

    Example:
        SELECT SUM("Revenue") AS total_revenue
        → value = 1234567

    Multi-row/multi-column results remain in AnalysisResult.table.
    """

    if len(result.rows) != 1:
        return None

    if len(result.columns) != 1:
        return None

    column = result.columns[0]

    return result.rows[0].get(column)


def _analysis_explanation(
    result: StructuredResult,
) -> str:
    pieces: list[str] = []

    if result.explanation:
        pieces.append(
            result.explanation.strip()
        )

    if result.result_text:
        pieces.append(
            result.result_text.strip()
        )

    return "\n\n".join(
        piece
        for piece in pieces
        if piece
    )
