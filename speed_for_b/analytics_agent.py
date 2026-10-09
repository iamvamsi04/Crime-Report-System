"""Opt-in analytics tools using the shared billing agent lifecycle."""
from agent.billing_agent import BillingAgent, RecordingWorkbench, parameters
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
        super().__init__(extended)


class AnalyticsBillingAgent(BillingAgent):
    system_message = ANALYTICS_MESSAGE
    workbench_factory = AnalyticsWorkbench
