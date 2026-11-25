"""Standalone SDK agent for reviewing chart query plans."""

import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Mapping
from contextlib import ExitStack, redirect_stdout

import llm_gateway_sdk
from llm_gateway_sdk import runtime_config
from autogen_agentchat.agents import AssistantAgent
from autogen_core import CancellationToken
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient

from agent.prompts.chart_review_policy import CHART_REVIEW_POLICY
from config import settings
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


class ReviewerConfigurationError(RuntimeError):
    """A startup configuration error safe to display in the server terminal."""


class ChartSQLReviewer(AssistantAgent):
    """Review read-only chart plans with a model dedicated to this reviewer."""

    def __init__(self, model_client, *, gateway_name="chart_sql_reviewer",
                 telemetry_enabled=False):
        super().__init__("chart_sql_reviewer", model_client=model_client,
                         system_message=CHART_REVIEW_POLICY)
        self.client = model_client
        self.gateway_name = gateway_name
        self.telemetry_enabled = telemetry_enabled

    @classmethod
    def from_gateway(cls):
        """Start the SDK for this process and let it route the reviewer's requests."""
        if not settings.LLM_GATEWAY_ENABLED:
            raise ReviewerConfigurationError("Configure LLM gateway credentials.")
        name = os.getenv("LLM_GATEWAY_CHART_REVIEWER_NAME", "").strip() or "chart_sql_reviewer"
        if name == settings.AGENT_NAME:
            raise ReviewerConfigurationError("Set LLM_GATEWAY_CHART_REVIEWER_NAME to a different name from LLM_GATEWAY_AGENT_NAME.")
        llm_gateway_sdk.init(agent_name=name, auto_detect_agent=False)
        config = runtime_config.current_config()
        if config is not None and name not in (config.get("agents") or {}):
            raise ReviewerConfigurationError(f"Register {name!r} in Config Server for this gateway service.")

        platform = (config or {}).get("platform") or {}
        telemetry = _start_telemetry(platform)
        client = AzureOpenAIChatCompletionClient(
            azure_endpoint="https://llm-gateway.invalid",
            azure_deployment="llm-gateway",
            api_key="routed-by-llm-gateway",
            api_version=settings.AZURE_OPENAI_API_VERSION or "2024-06-01",
            model=settings.AZURE_OPENAI_MODEL or "gpt-4o",
            model_info={"vision": False, "function_calling": False, "json_output": False,
                        "family": "unknown", "structured_output": False},
            timeout=settings.CHAT_TIMEOUT,
            max_retries=0,
        )
        return cls(client, gateway_name=name, telemetry_enabled=telemetry)

    async def review(self, reads, *, schema, user_request="", cancellation_token=None,
                     diagnostics: Mapping[int, str] | None = None):
        """Return the reviewed batch in its original order."""
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

        token = cancellation_token or CancellationToken()
        try:
            task = json.dumps({"request": user_request, "schema": schema,
                               "chart_reads": list(candidates.values())},
                              ensure_ascii=False, separators=(",", ":"))
            print(f"Chart SQL reviewer called for {len(candidates)} chart queries.", file=sys.stderr, flush=True)
            result = await self.run(task=task, cancellation_token=token)
            return _apply_review(reads, candidates, result.messages[-1].content)
        except Exception as exc:
            log.warning("Chart SQL reviewer model call failed (%s). Using the original SQL.", type(exc).__name__)
            return reads
        finally:
            await self.on_reset(token)

    async def review_request(self, request):
        """Apply the SDK context supplied by the local billing service."""
        context = request.get("context") or {}
        conversation_id = context.get("conversation_id")
        mode = context.get("mode", "admin")
        diagnostics = {int(index): reason for index, reason in (request.get("diagnostics") or {}).items()}
        with ExitStack() as stack:
            if conversation_id:
                stack.enter_context(llm_gateway_sdk.user_context(chat_id=conversation_id, session_id=conversation_id,
                                                 username=f"{mode}:{conversation_id}"))
            if self.telemetry_enabled:
                from opentelemetry import trace

                tracer = trace.get_tracer(self.gateway_name)
                stack.enter_context(tracer.start_as_current_span("chart_sql_review", attributes={
                    "billing.conversation_id": conversation_id or "",
                    "billing.request_id": context.get("request_id") or "",
                    "billing.mode": mode,
                }))
            return await self.review(request["reads"], schema=request["schema"],
                                     user_request=request.get("user_request", ""),
                                     diagnostics=diagnostics)

    async def close(self):
        await self.client.close()


def _start_telemetry(platform):
    endpoint = platform.get("collector_url") or settings.OTEL_ENDPOINT
    if not endpoint or not runtime_config.telemetry_enabled():
        return False
    try:
        import openlit
    except ImportError:
        log.warning("Chart SQL reviewer telemetry requires OpenLIT.")
        return False
    openlit.init(otlp_endpoint=endpoint, otlp_headers=llm_gateway_sdk.otlp_headers() or None,
                 application_name=(os.getenv("AGENTIC_SERVICE_NAME") or os.getenv("LLM_GATEWAY_SERVICE_NAME")
                                   or "chart_sql_reviewer"))
    return True


async def _serve(output):
    try:
        reviewer = ChartSQLReviewer.from_gateway()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ReviewerConfigurationError) else type(exc).__name__
        print(f"Chart SQL reviewer startup failed. {detail}", file=sys.stderr, flush=True)
        print(json.dumps({"error": type(exc).__name__}), file=output, flush=True)
        return

    print(f"Chart SQL reviewer ready (agent={reviewer.gateway_name}).", file=sys.stderr, flush=True)
    print(json.dumps({"ready": True}), file=output, flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                request = json.loads(line)
                response = {"reads": await reviewer.review_request(request)}
            except Exception as exc:
                response = {"error": type(exc).__name__}
            print(json.dumps(response, separators=(",", ":")), file=output, flush=True)
    finally:
        await reviewer.close()


if __name__ == "__main__":
    protocol_output = sys.stdout
    with redirect_stdout(sys.stderr):
        asyncio.run(_serve(protocol_output))
