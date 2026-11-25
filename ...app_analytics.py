"""Send chart plans to the independently configured SDK agent."""

import asyncio
import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

from config import settings
from mcp_server.charts import CHART_TYPES, canonical_chart_type
from mcp_server.tools.database import schema_hint


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

    def begin_turn(self, started):
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
        return result["reads"]

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
                reviewed = await self._exchange({
                    "reads": [reads[index] for index in chart_indexes],
                    "schema": schema_hint("admin"),
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

        if not isinstance(reviewed, list) or len(reviewed) != len(chart_indexes):
            return arguments
        updated_reads = list(reads)
        originals = {}
        for index, edited in zip(chart_indexes, reviewed, strict=True):
            original = reads[index]
            if isinstance(edited, dict) and original != edited:
                originals[str(index)] = {"query": original["query"], "chart": original["chart"]}
                updated_reads[index] = edited
        if not originals:
            return arguments
        updated = {**arguments, "queries": updated_reads} if batched else updated_reads[0]
        return {**updated, "chart_originals": originals}
