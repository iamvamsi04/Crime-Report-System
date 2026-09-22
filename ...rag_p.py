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
