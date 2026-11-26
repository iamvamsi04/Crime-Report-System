"""Send chart plans to the independently configured SDK agent."""

import asyncio
import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

from config import settings
from mcp_server.charts import CHART_TYPES, canonical_chart_type
from mcp_server.tools.database import preflight_chart_queries, schema_hint


log = logging.getLogger("uvicorn.error.billing_performance")
_context = ContextVar("chart_review_context", default=None)


@contextmanager
def chart_review_context(*, conversation_id, request_id, mode):
    token = _context.set({"conversation_id": conversation_id, "request_id": request_id, "mode": mode})
    try:
        yield
    finally:
        _context.reset(token)


class ChartReviewProcess:
    """Keep one reviewer worker alive without sharing the billing model client."""

    def __init__(self):
        self._worker = None
        self._booting = False
        self._boot_deadline = 0.0
        self._budget = 0.0
        self._deadline = 0.0
        self.pending = {}
        self.allowed_chart_types = None

    def begin_turn(self, started):
        self.pending.clear()
        self.allowed_chart_types = None
        self._budget = 8.0
        reply_reserve = min(4, settings.CHAT_TIMEOUT / 4)
        self._deadline = started + settings.CHAT_TIMEOUT - reply_reserve

    async def start(self):
        if not settings.LLM_GATEWAY_ENABLED:
            return
        if self._worker is None or (self._worker.returncode is not None and not self._booting):
            try:
                self._worker = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "agent.chart_sql_reviewer",
                    cwd=str(settings.ROOT), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    limit=8 * 1024 * 1024,
                )
                self._booting = True
                self._boot_deadline = perf_counter() + max(60, settings.CHAT_TIMEOUT)
            except (OSError, NotImplementedError) as exc:
                log.warning("Chart SQL reviewer could not start (%s).", type(exc).__name__)

    async def close(self, *, force=False):
        worker, self._worker = self._worker, None
        self._booting = False
        if worker is not None:
            if not force and worker.returncode is None:
                worker.stdin.close()
                try:
                    await asyncio.wait_for(worker.wait(), timeout=2)
                    return
                except TimeoutError:
                    pass
            if worker.returncode is None:
                try:
                    worker.kill()
                except ProcessLookupError:
                    pass
            await worker.wait()

    async def _exchange(self, request):
        await self.start()
        if self._worker is None:
            return None
        while self._booting:
            line = await self._worker.stdout.readline()
            if not line:
                raise EOFError("The chart reviewer exited during startup.")
            try:
                status = json.loads(line)
            except ValueError:
                continue
            if not isinstance(status, dict):
                continue
            if status.get("ready") is True:
                self._booting = False
            elif status.get("error"):
                await self.close(force=True)
                return None
        self._worker.stdin.write(json.dumps(request, separators=(",", ":")).encode("ascii") + b"\n")
        await self._worker.stdin.drain()
        line = await self._worker.stdout.readline()
        if not line:
            raise EOFError("The chart reviewer exited before responding.")
        result = json.loads(line)
        if "error" in result:
            log.warning("Chart SQL reviewer could not process this batch (%s).", result["error"])
            return None
        return result

    async def review_arguments(self, arguments, *, user_request, chart_errors):
        batched = arguments.get("queries") is not None
        reads = arguments.get("queries") if batched else [arguments]
        if not isinstance(reads, list):
            return arguments
        diagnostics = {}
        chart_indexes = []
        for index, read in enumerate(reads):
            spec = read.get("chart") if isinstance(read, dict) else None
            kind = canonical_chart_type(spec.get("type")) if isinstance(spec, dict) else None
            if kind in CHART_TYPES and isinstance(read.get("query"), str):
                if kind in chart_errors:
                    diagnostics[len(chart_indexes)] = chart_errors[kind]
                chart_indexes.append(index)

        if not chart_indexes:
            if any(isinstance(read, dict) and read.get("chart") is not None for read in reads):
                log.warning("Chart SQL reviewer skipped. No supported chart query was supplied.")
            return arguments
        if not settings.LLM_GATEWAY_ENABLED:
            log.warning("Chart SQL reviewer skipped. Configure LLM gateway credentials to enable it.")
            return arguments
        started = perf_counter()
        timeout = min(self._budget, self._deadline - started)
        if timeout <= 0:
            log.info("Chart SQL reviewer skipped. The review deadline for this turn has passed.")
            return arguments
        try:
            async with asyncio.timeout(timeout):
                chart_reads = [reads[index] for index in chart_indexes]
                diagnostics.update(preflight_chart_queries(chart_reads))
                reviewed = await self._exchange({
                    "reads": chart_reads, "schema": schema_hint("admin"),
                    "user_request": user_request, "diagnostics": diagnostics,
                    "context": _context.get() or {"mode": "admin"},
                })
        except asyncio.CancelledError:
            await self.close(force=True)
            raise
        except TimeoutError:
            if self._booting and perf_counter() < self._boot_deadline:
                log.info("Chart SQL reviewer is still starting. Using the original SQL for this batch.")
            else:
                phase = "startup" if self._booting else "review"
                await self.close(force=True)
                log.warning("Chart SQL reviewer timed out during %s. Using the original SQL.", phase)
            return arguments
        except (OSError, EOFError, ValueError, TypeError, KeyError) as exc:
            await self.close(force=True)
            log.warning("Chart SQL reviewer failed (%s). Using the original SQL.", type(exc).__name__)
            return arguments
        finally:
            elapsed = perf_counter() - started
            self._budget -= elapsed
            log.info("billing_chart_review seconds=%.3f candidates=%s", elapsed, len(chart_indexes))

        rows = reviewed.get("reads") if isinstance(reviewed, dict) else None
        if not isinstance(rows, list) or len(rows) != len(chart_indexes):
            return arguments
        scope = reviewed.get("allowed_chart_types")
        if self.allowed_chart_types is None and isinstance(scope, list) and all(kind in CHART_TYPES for kind in scope):
            self.allowed_chart_types = set(scope)
        reasons = reviewed.get("feedback")
        updated_reads = list(reads)
        originals, feedback = {}, {}
        for slot, (index, edited) in enumerate(zip(chart_indexes, rows, strict=True)):
            original = reads[index]
            kind = canonical_chart_type(original["chart"].get("type"))
            if self.allowed_chart_types is not None and kind not in self.allowed_chart_types:
                continue
            reason = reasons.get(str(slot)) if isinstance(reasons, dict) else None
            diagnostic = diagnostics.get(slot)
            if isinstance(edited, dict) and (original != edited or reason or diagnostic):
                originals[str(index)] = {"query": original["query"], "chart": original["chart"]}
                updated_reads[index] = edited
                if diagnostic or reason:
                    feedback[str(index)] = " ".join(part for part in (diagnostic, reason) if isinstance(part, str))
        if not originals:
            return arguments
        updated = {**arguments, "queries": updated_reads} if batched else updated_reads[0]
        return {**updated, "chart_originals": originals, "chart_review_feedback": feedback}

    async def feedback_for(self, arguments, **options):
        for original, _, response in self.pending.values():
            if original == arguments:
                return response
        suggested = dict(await self.review_arguments(arguments, **options))
        reasons = suggested.pop("chart_review_feedback", {})
        originals = suggested.get("chart_originals")
        if not originals:
            return None
        reads = suggested.get("queries") or [suggested]
        issues = []
        for index, original in originals.items():
            replacement = reads[int(index)]
            issue = {"index": int(index), "reason": reasons.get(index) or "Adjust the query or chart fields as suggested."}
            for key in ("query", "chart"):
                if replacement[key] != original[key]:
                    issue[f"suggested_{key}"] = replacement[key]
            issues.append(issue)
        review_id = uuid4().hex
        response = {"id": review_id, "issues": issues}
        if self.allowed_chart_types is not None:
            response["requested_types"] = [kind for kind in CHART_TYPES if kind in self.allowed_chart_types]
        self.pending[review_id] = (arguments, suggested, response)
        return response

    def restrict_arguments(self, arguments, *, declared_types=None):
        allowed = self.allowed_chart_types if self.allowed_chart_types is not None else declared_types
        output = dict(arguments)
        unresolved = output.pop("unresolved_chart_indexes", ())
        if allowed is None and not unresolved:
            return output

        def included(read):
            spec = read.get("chart") if isinstance(read, dict) else None
            return allowed is None or spec is None or (
                isinstance(spec, dict) and canonical_chart_type(spec.get("type")) in allowed
            )

        if allowed is not None:
            output.pop("expected_chart_types", None)
            if allowed:
                output["expected_chart_types"] = [kind for kind in CHART_TYPES if kind in allowed]
        if output.get("queries") is None:
            return output if not unresolved and included(output) else None
        if not isinstance(output["queries"], list):
            return output
        retained = [(index, read) for index, read in enumerate(output["queries"])
                    if index not in unresolved and included(read)]
        if not retained:
            return None
        output["queries"] = [read for _, read in retained]
        originals = output.pop("chart_originals", {})
        remapped = {str(index): originals[str(source)] for index, (source, _) in enumerate(retained)
                    if str(source) in originals}
        if remapped:
            output["chart_originals"] = remapped
        return output

    def resolve_revision(self, review_id, arguments):
        if not isinstance(review_id, str) or review_id not in self.pending:
            return None
        original, suggested, _ = self.pending.pop(review_id)
        batched = original.get("queries") is not None
        reads = list(suggested["queries"]) if batched else [suggested]
        revised = arguments.get("queries")
        if revised is None and len(reads) == 1:
            revised = [arguments]
        if not isinstance(revised, list) or len(revised) != len(reads):
            revised = [None] * len(reads)

        unresolved = []
        originals = {index: dict(read) for index, read in suggested["chart_originals"].items()}
        for index, before in originals.items():
            slot = int(index)
            candidate = revised[slot]
            saved = original["queries"][slot] if batched else original
            changed = False
            if isinstance(candidate, dict) and candidate.get("parameters", saved.get("parameters")) == saved.get("parameters"):
                query = candidate.get("query", saved["query"])
                chart = candidate.get("chart", saved["chart"])
                if (isinstance(query, str) and query.strip() and len(query) <= 20000
                        and isinstance(chart, dict) and canonical_chart_type(chart.get("type")) == canonical_chart_type(saved["chart"].get("type"))
                        and (query != before["query"] or chart != before["chart"])):
                    suggested_read = {key: reads[slot][key] for key in ("query", "chart")}
                    if suggested_read != before and suggested_read != {"query": query, "chart": chart}:
                        before["reviewed"] = suggested_read
                    reads[slot] = {**reads[slot], "query": query, "chart": chart}
                    changed = True
            if not changed and all(reads[slot].get(key) == before[key] for key in ("query", "chart")):
                unresolved.append(slot)
        output = {**suggested, "queries": reads} if batched else reads[0]
        return {**output, "chart_originals": originals, "unresolved_chart_indexes": unresolved}
