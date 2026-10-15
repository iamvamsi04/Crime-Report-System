import asyncio
import json
import os
import sys
import logging
from time import perf_counter
from copy import deepcopy
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_core.model_context import UnboundedChatCompletionContext
from autogen_core.models import UserMessage, AssistantMessage
from autogen_core.tools import ToolResult, TextResultContent
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams
from config import settings
from agent.system_message import SYSTEM_MESSAGE
from agent.admin_message import ADMIN_MESSAGE
from agent.task_policy import TASK_POLICY, CUSTOMER_TASK_POLICY
from mcp_server.charts import chart_from_result
from mcp_server.tools.database import schema_hint
from mcp_server.access import ADMIN_TOOLS, CUSTOMER_TOOLS, SCOPE_FIELDS
from context.reply_text import REPLY_POLICY, plain_reply
from context.shopping_state import ShoppingTracker
from agent.shopping_policy import shopping_prompt
from agent.continuation import POLICY as CONTINUATION_POLICY, eligible, compact_messages, valid_final


log = logging.getLogger("uvicorn.error.billing_performance")

# Only these tools are allowed together in a model-selected batch. Unknown and
# mutating tools fail closed before any member of that batch is dispatched.
BATCH_READ_TOOLS = frozenset({'execute_sql_query', 'compare_products',
    'find_cheaper_alternatives', 'billing_analysis', 'billing_anomalies'})
BATCH_POLICY = ('\nYou may request multiple independent read-only tools in one round. '
    'Use a later round when arguments depend on an earlier result. '
    'Request writes and invoice generation one tool at a time; never batch them.\n')


class TimedAzureClient(AzureOpenAIChatCompletionClient):
    """Measure SDK requests without logging prompts, identifiers or credentials."""
    async def create(self, *args, **kwargs):
        started = perf_counter()
        try:
            workbench = getattr(self, 'batch_workbench', None)
            if workbench is not None:
                kwargs['extra_create_args'] = {**kwargs.get('extra_create_args', {}),
                                               'parallel_tool_calls': True}
            messages = args[0] if args else kwargs.get('messages', [])
            compact_policy = getattr(self, 'continuation_policy', None)
            use_compact = bool(compact_policy and workbench is not None
                               and kwargs.get('tool_choice') != 'none' and eligible(messages))
            async def invoke(compact):
                call_args, call_kwargs = args, dict(kwargs)
                if compact:
                    attachments = {'charts':[{'title':c['title'],'type':c['type']} for c in workbench.charts],
                                   'invoices':workbench.downloads}
                    reduced = compact_messages(messages, compact_policy + '\nATTACHMENTS: ' + json.dumps(attachments))
                    if args:
                        call_args = (reduced, *args[1:])
                    else:
                        call_kwargs['messages'] = reduced
                before = perf_counter()
                value = await super(TimedAzureClient, self).create(*call_args, **call_kwargs)
                log.info('billing_model_phase phase=%s seconds=%.3f input_tokens=%s output_tokens=%s',
                         'compact_answer' if compact else 'full', perf_counter()-before,
                         value.usage.prompt_tokens, value.usage.completion_tokens)
                return value
            result = await invoke(use_compact)
            if use_compact and not valid_final(result, workbench.mode):
                # No tool suggested under reduced policy executes until full
                # planning has reconsidered the unchanged question and evidence.
                log.info('billing_compact fallback=full_planning')
                result = await invoke(False)
            if workbench is not None:
                workbench.prepare_batch(result.content)
            usage = result.usage
            log.info("billing_model seconds=%.3f input_tokens=%s output_tokens=%s",
                     perf_counter() - started, usage.prompt_tokens, usage.completion_tokens)
            return result
        except BaseException:
            log.info("billing_model seconds=%.3f status=failed", perf_counter() - started)
            raise


def parameters():
    return StdioServerParams(command=sys.executable, args=["-m", "mcp_server.server"],
                            cwd=str(settings.ROOT), env={**os.environ,
                            "BILLING_DB": str(settings.BILLING_DB),
                            "INVOICE_DIR": str(settings.INVOICE_DIR)}, read_timeout_seconds=60)


def decode_result(result):
    for block in result.result:
        content = getattr(block, "content", None)
        if isinstance(content, str):
            try:
                value = json.loads(content)
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
    return {"ok": False, "error": "Tool returned an unreadable response."}


class RecordingWorkbench(McpWorkbench):
    def __init__(self, server_params=None):
        super().__init__(server_params or parameters())
        self.downloads = []
        self.charts = []
        self.failed = False
        self._tool_schemas = None
        self.mode = 'customer'
        self.customer_id = None
        self.shopping_tracker = None
        self.tool_seconds = 0.0
        self.tool_calls = 0
        self._batch_blocked = False
        self._dispatch_lock = asyncio.Lock()

    def prepare_batch(self, content):
        calls = content if isinstance(content, list) else []
        self._batch_blocked = len(calls) > 1 and any(
            getattr(call, 'name', None) not in BATCH_READ_TOOLS for call in calls)
        if len(calls) > 1:
            log.info('billing_batch calls=%s allowed=%s', len(calls), not self._batch_blocked)

    async def list_tools(self):
        # Our server registers a fixed tool set. Cache schemas, never billing data.
        if self._tool_schemas is None:
            self._tool_schemas = await super().list_tools()
        allowed = ADMIN_TOOLS if self.mode == 'admin' else CUSTOMER_TOOLS
        schemas = [deepcopy(s) for s in self._tool_schemas if s['name'] in allowed]
        for schema in schemas:
            parameters = schema.get('parameters', {})
            if self.mode == 'customer' and schema['name'] == 'execute_sql_query':
                schema['description'] = schema['description'].split('For requested admin visuals', 1)[0].rstrip()
                parameters.get('properties', {}).pop('chart', None)
                if 'required' in parameters:
                    parameters['required'] = [f for f in parameters['required'] if f != 'chart']
            for field in SCOPE_FIELDS:
                parameters.get('properties', {}).pop(field, None)
            if 'required' in parameters:
                parameters['required'] = [f for f in parameters['required'] if f not in SCOPE_FIELDS]
        return schemas

    async def stop(self):
        self._tool_schemas = None
        await super().stop()

    async def call_tool(self, name, arguments=None, cancellation_token=None, **kwargs):
        if self._batch_blocked:
            return ToolResult(name=name, is_error=True, result=[TextResultContent(content=json.dumps({
                'ok': False, 'error': 'No tools in this batch were executed. Request writes and '
                'invoice generation one at a time. Only independent read-only tools may be batched.'}))])
        # AutoGen dispatches a returned batch concurrently. Preserve tool/result
        # ordering for shopping navigation and checkpoints; savings come from
        # fewer model round trips, not concurrent database access.
        async with self._dispatch_lock:
            return await self._call_tool(name, arguments, cancellation_token, **kwargs)

    async def _call_tool(self, name, arguments=None, cancellation_token=None, **kwargs):
        # The model cannot choose its execution role or customer scope.
        arguments = {**(arguments or {}), 'execution_mode': self.mode,
                     'execution_customer_id': self.customer_id}
        started = perf_counter()
        try:
            result = await super().call_tool(name, arguments, cancellation_token, **kwargs)
        finally:
            elapsed = perf_counter() - started
            self.tool_seconds += elapsed
            self.tool_calls += 1
            log.info("billing_tool tool=%s seconds=%.3f", name, elapsed)
        self.failed = self.failed or result.is_error
        if name == 'execute_sql_query' and self.mode == 'admin':
            data = decode_result(result)
            if data.get('ok') and data.get('chart'):
                chart = chart_from_result(data)
                if chart not in self.charts:
                    if len(self.charts) < 6:
                        self.charts.append(chart)
                    else:
                        data.pop('chart')
                        data['chart_error'] = 'Six-chart limit reached. Ask for remaining charts in another turn.'
                        for block in result.result:
                            if isinstance(getattr(block,'content',None),str):
                                block.content = json.dumps(data)
                                break
        if name == "download_invoice":
            data = decode_result(result)
            if data.get("ok") and data.get("ready"):
                self.downloads.append({"invoice_number": data["invoice_number"], "customer_id": data["customer_id"]})
        if name == 'manage_customer' and self.mode == 'customer' and self.customer_id is None:
            data = decode_result(result)
            if data.get('ok') and data.get('action') == 'create':
                self.customer_id = data['customer_id']
        if self.shopping_tracker is not None and self.mode == 'customer':
            self.shopping_tracker.observe(name, arguments, decode_result(result), self.customer_id)
        return result


def create_model_client(**client_options):
    error = settings.azure_configuration_error()
    if error:
        raise ValueError(error)
    if settings.AZURE_REASONING_EFFORT:
        client_options.setdefault('reasoning_effort', settings.AZURE_REASONING_EFFORT)
    # The deployment controls the Azure route; model is its underlying model name.
    # This assistant needs text chat and tool calling, not vision or structured output.
    return TimedAzureClient(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        model=settings.AZURE_OPENAI_MODEL,
        model_info={"vision": False, "function_calling": True, "json_output": False,
                    "family": "unknown", "structured_output": False},
        parallel_tool_calls=False,
        timeout=settings.CHAT_TIMEOUT,
        max_retries=0,
        **client_options,
    )


class BillingAgent:
    system_message = SYSTEM_MESSAGE
    workbench_factory = RecordingWorkbench

    def __init__(self):
        self._client = None
        self._workbench = None
        self._managed = False
        self._lock = asyncio.Lock()

    async def start(self):
        """Warm the MCP process once during API startup; Azure stays lazy."""
        async with self._lock:
            await self._get_workbench()
            self._managed = True

    async def _get_workbench(self):
        if self._workbench is None:
            self._workbench = self.workbench_factory()
            try:
                await self._workbench.start()
                await self._workbench.list_tools()
            except BaseException:
                await self._reset()
                raise
        return self._workbench

    async def _reset(self):
        workbench, client = self._workbench, self._client
        self._workbench = self._client = None
        try:
            if workbench is not None:
                await workbench.stop()
        finally:
            if client is not None:
                await client.close()

    async def close(self):
        async with self._lock:
            self._managed = False
            await self._reset()

    def validate_configuration(self):
        error = settings.azure_configuration_error()
        if error:
            raise ValueError(error)

    async def chat(self, message, history, customer_id, mode='customer', shopping_state=None, state_callback=None):
        started = perf_counter()
        async with self._lock:
            acquired = perf_counter()
            workbench = None
            try:
                workbench = await self._get_workbench()
                workbench.tool_seconds = 0.0
                workbench.tool_calls = 0
                workbench.downloads.clear()
                workbench.charts.clear()
                workbench.mode, workbench.customer_id = mode, customer_id
                workbench.prepare_batch(None)
                tracker = ShoppingTracker(shopping_state, customer_id, state_callback) if mode == 'customer' else None
                workbench.shopping_tracker = tracker
                memory_prompt = shopping_prompt(tracker.state) if tracker else ''
                # A new user turn invalidates the previous pending proposal as a
                # future approval target; it remains visible in this turn's prompt.
                if tracker:
                    tracker.state['pending_confirmation'] = None
                    tracker.state['next_question'] = None
                    tracker.stage()
                    if state_callback:
                        state_callback(deepcopy(tracker.state), customer_id)
                if self._client is None:
                    self._client = create_model_client()
                if isinstance(self._client, TimedAzureClient):
                    self._client.batch_workbench = workbench
                    self._client.continuation_policy = (CONTINUATION_POLICY + REPLY_POLICY
                        + f'\nTrusted mode: {mode}. Associated customer_id: {customer_id or "unset"}.\n'
                        + (memory_prompt if tracker else 'Return plain English without a shopping JSON envelope.'))
                previous = [UserMessage(content=m["content"], source="user") if m["role"] == "user"
                            else AssistantMessage(content=m["content"], source="billing_agent") for m in history]
                # Agent/model context is fresh per turn; only transports are shared.
                agent = AssistantAgent("billing_agent", model_client=self._client, workbench=workbench,
                        model_context=UnboundedChatCompletionContext(initial_messages=previous),
                        system_message=(ADMIN_MESSAGE if mode == 'admin' else self.system_message) + (TASK_POLICY if mode == 'admin' else CUSTOMER_TASK_POLICY) + REPLY_POLICY + BATCH_POLICY + '\n' + schema_hint(mode) + f"\nAssociated customer_id: {customer_id or 'unset'}\n" + memory_prompt,
                        reflect_on_tool_use=True, max_tool_iterations=settings.MAX_TOOL_ITERATIONS)
                result = await agent.run(task=message)
                final = result.messages[-1]
                if not isinstance(final, TextMessage):
                    raise RuntimeError("Agent did not produce a final text answer.")
                reply = tracker.finish(final.content) if tracker else plain_reply(final.content)
                return {"response": reply, "invoices": list(workbench.downloads),
                        "charts": list(workbench.charts),
                        "associated_customer_id": workbench.customer_id,
                        **({'shopping_state': deepcopy(tracker.state)} if tracker else {})}
            except BaseException:
                # Discard failed/cancelled transports without retrying the turn.
                await self._reset()
                raise
            finally:
                log.info("billing_turn mode=%s total_seconds=%.3f lock_wait_seconds=%.3f tool_calls=%s tool_seconds=%.3f",
                         mode, perf_counter() - started, acquired - started,
                         workbench.tool_calls if workbench else 0,
                         workbench.tool_seconds if workbench else 0.0)
                if self._workbench is not None:
                    self._workbench.shopping_tracker = None
                if not self._managed or (self._workbench is not None and self._workbench.failed):
                    await self._reset()

    async def invoice(self, invoice_number, customer_id):
        if self._managed:
            async with self._lock:
                try:
                    workbench = await self._get_workbench()
                    workbench.mode, workbench.customer_id = 'customer', customer_id
                    result = decode_result(await workbench.call_tool('download_invoice',
                        {'invoice_number': invoice_number, 'customer_id': customer_id}))
                    if workbench.failed:
                        await self._reset()
                    return result
                except BaseException:
                    await self._reset()
                    raise
        async with McpWorkbench(parameters()) as workbench:
            return decode_result(await workbench.call_tool("download_invoice",
                {"invoice_number": invoice_number, "customer_id": customer_id,
                 "execution_mode": "customer", "execution_customer_id": customer_id}))
