"""Opt-in agent extension; original agent, prompts and MCP server files are untouched."""
from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.messages import TextMessage
from autogen_core.model_context import UnboundedChatCompletionContext
from autogen_core.models import UserMessage, AssistantMessage
from autogen_ext.tools.mcp import McpWorkbench

from agent.billing_agent import BillingAgent, RecordingWorkbench, create_model_client, parameters
from agent.system_message import SYSTEM_MESSAGE

ANALYTICS_MESSAGE = SYSTEM_MESSAGE.replace(
    "Read questions MUST use execute_sql_query;", "Use execute_sql_query for simple lookups;").replace(
    "Use the six tools only.", "Use only the registered billing tools.") + """

Two additional read-only tools are available:
- billing_analysis: use for billing analysis, cost breakdowns, invoice totals,
  net billed after ledger refunds, top customers/products and current-cost estimates.
- billing_anomalies: use for inconsistent invoices, anomalies and cost optimization suggestions.
Prefer these specialized tools for those requests. Pass the user's explicit customer,
order and date filters; do not silently restrict a requested company-wide analysis.
When a request says 'my', use the associated customer if available or ask which customer.
Explain monetary estimates and limits: current baseprice is not historical cost;
ledger refunds are not verified payments; a flagged invoice is a review candidate.
Do not treat every flag as an error or claim guaranteed savings. Respect truncated lists.
Report only findings and suggestions supported by the tool results. These tools do not modify data.
"""


class AnalyticsWorkbench(RecordingWorkbench):
    def __init__(self):
        extended = parameters().model_copy(update={"args": ["-m", "mcp_server.server_analytics"]})
        McpWorkbench.__init__(self, extended)
        self.downloads = []


class AnalyticsBillingAgent(BillingAgent):
    async def chat(self, message, history, customer_id):
        client = create_model_client()
        try:
            async with AnalyticsWorkbench() as workbench:
                previous = [UserMessage(content=m["content"], source="user") if m["role"] == "user"
                            else AssistantMessage(content=m["content"], source="billing_agent") for m in history]
                agent = AssistantAgent("billing_agent", model_client=client, workbench=workbench,
                    model_context=UnboundedChatCompletionContext(initial_messages=previous),
                    system_message=ANALYTICS_MESSAGE + f"\nAssociated customer_id: {customer_id or 'unset'}",
                    reflect_on_tool_use=True, max_tool_iterations=8)
                response = (await agent.run(task=message)).messages[-1]
                if not isinstance(response, TextMessage):
                    raise RuntimeError("Agent did not produce a final text answer.")
                return {"response": response.content, "invoices": workbench.downloads}
        finally:
            await client.close()
