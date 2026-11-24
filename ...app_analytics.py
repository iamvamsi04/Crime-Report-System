"""Review chart query plans through the billing application's SDK model route."""

import json
import logging
import re
from collections.abc import Mapping

from autogen_agentchat.agents import AssistantAgent

from agent.prompts.chart_review_policy import CHART_REVIEW_POLICY
from mcp_server.charts import CHART_TYPES, SPEC_FIELDS, canonical_chart_type


log = logging.getLogger("uvicorn.error.billing_performance")

_SQL_IDENTIFIER_CHAR = r"[\w$\x80-\U0010ffff]"
_SQL_PARAMETER_TOKENS = re.compile(
    r"""--[^\n]*|/\*[\s\S]*?(?:\*/|\Z)|'(?:''|[^'])*'|"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\]"""
    rf"|(?P<parameter>\?[0-9]*|[:@$]{_SQL_IDENTIFIER_CHAR}+(?:::{_SQL_IDENTIFIER_CHAR}+)*"
    rf"(?:\([^\s)]*\))?)|{_SQL_IDENTIFIER_CHAR}+"
)


def _placeholders(query):
    return tuple(match["parameter"] for match in _SQL_PARAMETER_TOKENS.finditer(query)
                 if match["parameter"] is not None)


def _replacement_chart(original, proposed):
    if not isinstance(proposed, dict) or set(proposed) - SPEC_FIELDS:
        return False
    kind = canonical_chart_type(proposed.get("type"))
    return (
        kind in CHART_TYPES
        and kind == canonical_chart_type(original.get("type"))
        and isinstance(proposed.get("x"), str)
        and bool(proposed["x"].strip())
        and isinstance(proposed.get("y"), list)
        and all(isinstance(value, str) and value.strip() for value in proposed["y"])
        and all(isinstance(proposed[key], str) and proposed[key].strip()
                for key in ("group", "size", "end", "label", "x_type", "title") if key in proposed)
        and ("bins" not in proposed or type(proposed["bins"]) is int)
    )


def _apply_review(reads, selected_indexes, content):
    if not isinstance(content, str):
        return reads
    lines = content.strip().splitlines()
    if lines and lines[0] in ("```", "```json") and lines[-1] == "```":
        content = "\n".join(lines[1:-1])
    try:
        response = json.loads(content)
    except (ValueError, RecursionError):
        return reads
    if (not isinstance(response, dict) or set(response) != {"replacements"}
            or not isinstance(response["replacements"], list)):
        return reads

    patches = {}
    for patch in response["replacements"]:
        if not isinstance(patch, dict) or type(patch.get("index")) is not int:
            continue
        index = patch["index"]
        if index in selected_indexes:
            patches[index] = None if index in patches else patch

    for index, patch in patches.items():
        if patch is None or set(patch) - {"index", "query", "chart"}:
            continue
        original = reads[index]
        query = patch.get("query", original["query"])
        chart = patch.get("chart", original["chart"])
        if not isinstance(query, str) or not query.strip() or len(query) > 20000:
            continue
        if query != original["query"] and _placeholders(query) != _placeholders(original["query"]):
            continue
        if "chart" in patch and not _replacement_chart(original["chart"], chart):
            continue
        try:
            query.encode("utf-8")
            json.dumps(chart, ensure_ascii=False).encode("utf-8")
        except UnicodeError:
            continue
        if query != original["query"] or chart != original["chart"]:
            reads[index] = {**original, "query": query, "chart": chart}
    return reads


async def review_chart_reads(reads, *, model_client, schema, user_request="", cancellation_token=None,
                             diagnostics: Mapping[int, str] | None = None):
    """Return chart reads in their original order and keep any unreviewed plan."""
    reads = list(reads)
    candidates = {}
    for index, read in enumerate(reads):
        if (not isinstance(read, dict) or not isinstance(read.get("query"), str)
                or not isinstance(read.get("chart"), dict)
                or canonical_chart_type(read["chart"].get("type")) not in CHART_TYPES):
            continue
        candidate = {key: read[key] for key in ("query", "parameters", "chart") if key in read}
        if diagnostics and index in diagnostics:
            candidate["previous_error"] = diagnostics[index]
        candidates[index] = {"index": index, **candidate}
    if not candidates:
        return reads

    try:
        task = json.dumps({"request": user_request, "chart_reads": list(candidates.values())},
                          ensure_ascii=False, separators=(",", ":"))
        reviewer = AssistantAgent(
            "chart_sql_reviewer", model_client=model_client,
            system_message=f"{CHART_REVIEW_POLICY}\nCurrent permitted schema:\n{schema}",
        )
        result = await reviewer.run(task=task, cancellation_token=cancellation_token)
        content = result.messages[-1].content
    except Exception as exc:
        log.info("billing_chart_review status=fallback error_type=%s", type(exc).__name__)
        return reads
    return _apply_review(reads, candidates, content)
