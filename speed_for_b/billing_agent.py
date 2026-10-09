import asyncio
import json
import os
import sys
from copy import deepcopy
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_core.model_context import UnboundedChatCompletionContext
from autogen_core.models import UserMessage, AssistantMessage
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams
from config import settings
from agent.system_message import SYSTEM_MESSAGE


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
        self.failed = False
        self._tool_schemas = None

    async def list_tools(self):
        # Our server registers a fixed tool set. Cache schemas, never billing data.
        if self._tool_schemas is None:
            self._tool_schemas = await super().list_tools()
        return deepcopy(self._tool_schemas)

    async def stop(self):
        self._tool_schemas = None
        await super().stop()

    async def call_tool(self, name, arguments=None, cancellation_token=None, **kwargs):
        result = await super().call_tool(name, arguments, cancellation_token, **kwargs)
        self.failed = self.failed or result.is_error
        if name == "download_invoice":
            data = decode_result(result)
            if data.get("ok") and data.get("ready"):
                self.downloads.append({"invoice_number": data["invoice_number"], "customer_id": data["customer_id"]})
        return result


def create_model_client(**client_options):
    error = settings.azure_configuration_error()
    if error:
        raise ValueError(error)
    # The deployment controls the Azure route; model is its underlying model name.
    # This assistant needs text chat and tool calling, not vision or structured output.
    return AzureOpenAIChatCompletionClient(
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

    async def chat(self, message, history, customer_id):
        async with self._lock:
            try:
                if self._client is None:
                    self._client = create_model_client()
                workbench = await self._get_workbench()
                workbench.downloads.clear()
                previous = [UserMessage(content=m["content"], source="user") if m["role"] == "user"
                            else AssistantMessage(content=m["content"], source="billing_agent") for m in history]
                # Agent/model context is fresh per turn; only transports are shared.
                agent = AssistantAgent("billing_agent", model_client=self._client, workbench=workbench,
                        model_context=UnboundedChatCompletionContext(initial_messages=previous),
                        system_message=self.system_message + f"\nAssociated customer_id: {customer_id or 'unset'}",
                        reflect_on_tool_use=True, max_tool_iterations=8)
                result = await agent.run(task=message)
                final = result.messages[-1]
                if not isinstance(final, TextMessage):
                    raise RuntimeError("Agent did not produce a final text answer.")
                return {"response": final.content, "invoices": list(workbench.downloads)}
            except BaseException:
                # Discard failed/cancelled transports without retrying the turn.
                await self._reset()
                raise
            finally:
                if not self._managed or (self._workbench is not None and self._workbench.failed):
                    await self._reset()

    async def invoice(self, invoice_number, customer_id):
        async with McpWorkbench(parameters()) as workbench:
            return decode_result(await workbench.call_tool("download_invoice",
                {"invoice_number": invoice_number, "customer_id": customer_id}))
