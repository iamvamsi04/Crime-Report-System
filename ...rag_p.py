
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
