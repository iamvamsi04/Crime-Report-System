
async def execute_csv_step(
    question: str,
    document_ids: list[str],
    document_map: dict[str, Document],
    execution_flow: list[str],
) -> tuple[
    list[Evidence],
    list[Source],
]:
    """
    Execute DuckDB analysis for CSV/Excel documents.

    Each selected tabular document is queried independently.
    This keeps the generated SQL scoped to a known source.
    """

    evidence: list[Evidence] = []
    sources: list[Source] = []

    for document_id in document_ids:

        document = document_map.get(
            document_id
        )

        if document is None:
            continue

        execution_flow.append(
            f"Analyzing {document.filename} "
            "with DuckDB."
        )

        try:
            result = await answer_csv_question(
                question=question,
                document=document,
            )

        except Exception:
            log.exception(
                "CSV analysis failed for %s",
                document.filename,
            )

            execution_flow.append(
                f"DuckDB analysis failed for "
                f"{document.filename}."
            )

            raise RuntimeError(
                f"Could not analyze {document.filename}."
            )

        execution_flow.append(
            f"Executed a DuckDB query against "
            f"{document.filename}."
        )

        result_dict = result.model_dump(
            mode="json"
        )

        evidence.append(
            Evidence(
                source_type="csv",
                document_id=document.id,
                filename=document.filename,
                content=format_csv_result(
                    result_dict
                ),
                metadata={
                    "sql": result.sql,
                    "columns": result.columns,
                    "row_count": result.row_count,
                },
            )
        )

        sources.append(
            build_csv_source(
                document,
                result_dict,
            )
        )

    return evidence, sources
