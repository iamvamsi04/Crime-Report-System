def format_csv_result(
    result: dict[str, Any],
) -> str:
    """
    Convert a CSV/DuckDB analysis result into text for the
    final answer generator.
    """

    filename = result.get(
        "filename",
        "Unknown CSV",
    )

    sql = result.get(
        "sql",
        "",
    )

    columns = result.get(
        "columns",
        [],
    )

    rows = result.get(
        "rows",
        [],
    )

    lines = [
        f"CSV source: {filename}",
        "Source type: csv",
        "",
        "DuckDB SQL used:",
        sql,
        "",
        "Result columns:",
        ", ".join(
            str(column)
            for column in columns
        ),
        "",
        "Result rows:",
    ]

    if not rows:
        lines.append(
            "No rows were returned."
        )
    else:
        for row in rows:
            lines.append(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    default=str,
                )
            )

    return "\n".join(lines)
