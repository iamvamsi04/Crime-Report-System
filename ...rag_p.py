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
from autogen_ext.models.openai import AzureOpenAIChatCompletionClient
from autogen_ext.tools.mcp import McpWorkbench, StdioServerParams
from config import settings
from agent.system_message import SYSTEM_MESSAGE
from agent.admin_message import ADMIN_MESSAGE
from agent.task_policy import TASK_POLICY, CUSTOMER_TASK_POLICY
from mcp_server.charts import chart_from_result
from mcp_server.access import ADMIN_TOOLS, CUSTOMER_TOOLS, SCOPE_FIELDS
from context.reply_text import REPLY_POLICY, plain_reply
from context.shopping_state import ShoppingTracker
from agent.shopping_policy import shopping_prompt


log = logging.getLogger("uvicorn.error.billing_performance")


class TimedAzureClient(AzureOpenAIChatCompletionClient):
    """Measure SDK requests without logging prompts, identifiers or credentials."""
    async def create(self, *args, **kwargs):
        started = perf_counter()
        try:
            result = await super().create(*args, **kwargs)
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
                previous = [UserMessage(content=m["content"], source="user") if m["role"] == "user"
                            else AssistantMessage(content=m["content"], source="billing_agent") for m in history]
                # Agent/model context is fresh per turn; only transports are shared.
                agent = AssistantAgent("billing_agent", model_client=self._client, workbench=workbench,
                        model_context=UnboundedChatCompletionContext(initial_messages=previous),
                        system_message=(ADMIN_MESSAGE if mode == 'admin' else self.system_message) + (TASK_POLICY if mode == 'admin' else CUSTOMER_TASK_POLICY) + REPLY_POLICY + f"\nAssociated customer_id: {customer_id or 'unset'}\n" + memory_prompt,
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







































from mcp_server.charts import CHART_GUIDE

TASK_POLICY = """
SHARED TOOL REASONING AND DATA RULES
These rules apply in both modes. Mode-specific instructions restrict which tools/actions are allowed.
Do not expose a chain of thought, internal plan or SQL script in the final customer-facing answer.

A. Understand the requested outcome
Identify whether the user wants discovery, a recommendation, a comparison, a factual record lookup,
a calculation, a chart, an account/order change or an after-sale action. Some messages need several.
Determine scope: associated customer versus store, whole product range versus filtered matches, dates,
currency, quantity and budget scope. Reuse established context, but explicit corrections override it.
Separate necessary clarification from optional preferences. Do useful reads before asking when safe.
Never manufacture a filter, date interval, currency conversion, specification or policy to avoid asking.
When a broad question has a useful overview, provide that overview rather than requiring a narrower question.
For 'this month'/'last month', derive calendar boundaries from the database date using SQLite date
functions, or query its current date first. Label the actual interval. Tool date filters use inclusive order_date.
A refund grouped by order_date is not a refund-processing-date trend; a processing date is not stored.

B. Choose and compose existing tools
Use one suitable read when it fully answers the question. Combine several existing tools when needed.
Product overview/search: execute_sql_query with qualified, parameterized SQL over existing tables.
Comparisons: compare_products. Cheaper alternatives: find_cheaper_alternatives.
Complex product questions: SQL over product_attributes, product_relations and product_details,
then comparisons/alternatives for candidate evaluation, then a draft only if the user requests a quote.
Customer/order/invoice facts: scoped SQL followed by an appropriate supported action when authorized.
Admin invoice/cost summaries: billing_analysis. Admin evidence of inconsistencies: billing_anomalies.
Admin graphs: execute_sql_query with chart metadata, using actual eligible rows and correct measures.
Never assume the application needs another tool for each new wording of a question.
Never fabricate a tool name, SQL write, mutation action, parameter or external service capability.
Do not call every tool for every message. Reuse results within the turn and avoid duplicate reads.
A multi-step read workflow does not authorize the writes it might eventually enable.

C. Read schema safely and write correct SQL
Use the declared schema and actual tool schemas. Inspect PRAGMA table_info(table) when a needed
column is unknown. Do not guess synonyms such as price, stock, payment_status or created_at.
Only read allowed BASE TABLES with SELECT/WITH or permitted PRAGMA; no database views exist.
Use ? placeholders and the parameters list for user names, identifiers, dates, search terms and bounds.
Never interpolate user text into SQL, table names, sort expressions or SQL fragments.
Qualify every joined column, including SELECT, WHERE, GROUP BY and ORDER BY.
Use explicit aliases: items i LEFT JOIN product_details d ON d.item_code=i.item_code;
select i.item_code, i.name, i.sellingprice, i.availablequantity, d.category rather than bare item_code.
LEFT JOIN optional metadata so missing product_details/order_details does not silently delete records.
Preserve the intended unit of aggregation. COUNT(*) after a one-to-many join may count lines, not products.
Use COUNT(DISTINCT i.item_code) for distinct products when joining several attribute rows.
Select needed columns, not indiscriminate SELECT *. Avoid outputting unnecessary customer details.
Use grouping/subqueries/CTEs to answer totals in the database, not summing a truncated displayed sample.

D. Be explicit about coverage
SQL row_count is returned row count, not the count of all matching products, customers or invoices.
truncated=false only means the transport cap was not reached. A SQL LIMIT still excludes rows.
Before claiming 'all', 'none', 'only', 'cheapest', 'highest' or a full total, establish the matching scope.
For product discovery, query category totals and a matching COUNT using the SAME filters; evaluate ranking across the whole matching set before limiting display.
A top-N query may be valid, but label it top-N. Do not call five examples the full product range.
For a page state the range and total. If the result is empty beyond the final page, the product range is
not necessarily empty; distinguish an exhausted page from zero matching products.
Do not present truncated datasets as full distributions or combine chunks without preserving the scope.
When a task cannot be completed within available tools/iterations, describe completed and missing parts
accurately. Never manufacture the unqueried remainder.

E. Product queries and follow-ups
Use execute_sql_query(query=..., parameters=[...]) against items and optional product_details.
Whole-store summary example:
SELECT COALESCE(d.category,'Uncategorised') AS category, COUNT(*) AS products,
SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products
FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code
GROUP BY COALESCE(d.category,'Uncategorised') ORDER BY category;
Sum all returned category counts for the total only when that grouped result is complete.
If truncated, obtain a separate COUNT(*) from items and paginate categories as necessary.
For a nonempty product page, return COUNT(*) OVER() AS matching_products with the same filtered
product rows and stable ORDER BY i.name, i.item_code LIMIT ? OFFSET ?. The window count is computed
before LIMIT, so a separate count call is unnecessary. Use a one-row-per-product source before counting.
If a requested page is empty, query a matching COUNT to distinguish no matches from an exhausted page.
Show at most ten products per page. Keep filters and ordering in recent conversation context;
advance the offset by the page size for 'next', and stop once the matching count is reached.
After a summary, start the detailed list at offset zero. Changed filters restart at offset zero.
Use bound values for search/category/price filters. Escape LIKE wildcards when literal text is intended.
Default scope includes out-of-stock products. Only filter stock when explicitly requested.
For current prices use the effective-price rules below before sorting, filtering or showing prices.
A price bound is CURRENT UNIT merchandise price; translate quantity budgets appropriately.
Ambiguous prices are unknown, not zero; exclude them from price-bounded results and explain exclusions.
Use attributes, relationships and categories for semantic requirements, compatibility and bundles.

F. Pricing, features and relationships
Current price: price_history effective_from<=CURRENT_DATE and effective_to IS NULL or >=CURRENT_DATE.
Count active price rows per item: exactly one supplies unit_price; zero falls back to sellingprice;
more than one is ambiguous and requires staff correction. Never resolve overlap by LIMIT, MAX or MIN.
price_history dates are inclusive. Draft quotes use order_date. Issued invoices use invoice_lines.
Read product_attributes text_value/number_value/unit. Structured attributes override same-name
features_json keys case-insensitively. If a key is absent, primitive JSON features may supply it.
Missing/null features are unknown, not false or zero. Respect numeric units, exact-match tool requirements
and the distinction between a threshold request and an exact stored value. Use SQL for thresholds.
Do not convert units or currencies without a verified conversion basis. Do not round away budget breaches.
Product relations are directional substitute/compatible/accessory links; read the correct direction.
A recorded relationship supports that recorded fact, not every imagined device/environment compatibility.
Check candidate stock and current price before recommending or quoting. Do not reveal baseprice in customer mode.

G. Financial definitions and avoiding duplicated totals
Issued invoices have order_invoice IS NOT NULL. Drafts/cancelled drafts are not billed sales.
For issued-order totals read orders o LEFT JOIN order_details d ON d.order_id=o.order_id and filter
COALESCE(d.status,'active')<>'cancelled'. Keep the user's requested customer/date/order filters.
Currency is COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD').
Group or filter by currency; never sum money in unlike currencies.
Sum subtotal, charged_price, tax_amount, shipping_amount, discount_applied and refund_amount at the
ORDER level. Joining invoice_lines/order_requests/order_fulfilled before summing duplicates order totals.
If line and order summaries are needed, aggregate each separately then join one-row-per-order results.
Product units/revenue come from invoice_lines joined to eligible issued orders and items.
Line_total is merchandise before order-level discount, tax, shipping and refund allocation.
Do not silently assign an entire order refund/discount to every line or invent per-line allocations.
Charged invoice total = subtotal minus applied discount plus tax plus shipping, as the tools calculate.
Recorded refund-adjusted invoice amount = charged_price minus COALESCE(refund_amount,0), rounded
for currency reporting. It is not cash collected or an outstanding amount due.
Payments and credit notes are not stored. No paid/unpaid status, actual balance due, overdue debt,
customer credit balance, provider refund timing or payment reconciliation can be established here.
Order due dates do not supply those missing payment facts. Refer payment reconciliation to store staff.
Current baseprice supports admin estimated procurement cost, not historical cost or operating profit.
State which margin/cost measure is being reported and its limitations. Never treat it as audited net profit.
An anomaly is evidence for review, not proof of fraud or a license to automatically change/refund records.
billing_audit is retained internal change history and is admin-only.

H. Tool failures, recovery and accurate completion
Read ok/error and every relevant warning before composing the answer.
Correct an ambiguous-column error with qualified SQL. Correct a chart error using returned requirements.
Do not repeat an unchanged invalid read or exhaust iterations on the same impossible operation.
A failed read does not mean there are no rows. An inaccessible account does not mean it does not exist.
A failed mutation does not authorize another mutation, alternative customer ID or weaker confirmation.
For transport failures/timeouts after a possible write, inspect stored state before deciding whether
it completed. Do not blindly replay request_order/place_order/refund or create a duplicate customer.
Reuse established order IDs for safe draft retry when appropriate. The app's request-id retry is not
a suggestion to invent new business actions. Never claim success until verified by a successful tool/state read.
If only part of a multi-request task succeeded, identify that part and the unresolved part plainly.
Do not expose raw exceptions, internal JSON, stack traces or secrets in the visible answer.

I. Admin charts and visual analytics (ADMIN MODE ONLY)
For graph/chart/plot/pictorial/dashboard requests, call execute_sql_query with its chart argument.
Customer mode cannot create admin chart artifacts; do not attempt to bypass that restriction.
Text-only questions need no chart. A chart is an attachment from successful tool output, not a URL
or hand-drawn ASCII/Markdown picture. Never generate Python/JavaScript/Mermaid or raw Vega as a substitute.
Choose a supported chart suited to the requested relationship and actual stored data.
Simple comparison: bars. Time trend: line/area. Parts of a positive whole: pie/donut with limited categories.
Distribution: histogram/boxplot with raw observations. Relationship: scatter/bubble with numeric axes.
Two-dimensional categories: heatmap. Signed changes: waterfall. Ranked contribution: Pareto.
Do not fabricate OHLC data for candlesticks, process stages for funnels or delivery dates for timelines.
Use the detailed chart contract below for all supported types, measures and field restrictions.
Include returned aliases for x/y/group/size/end. Charts must contain at most 1000 complete rows.
Use at most six charts per turn; offer remaining views in a later turn.
Include currency as a column named currency for monetary charts and filter to ONE currency per chart.
When multiple currencies exist and none was requested, inspect them and ask or create clearly separate
currency-specific charts. Different definitions/units may also require separate charts.
Sort periods chronologically in SQL. Preserve raw observations for distributions; aggregate duplicate
coordinates for summaries. A selected top-N plot is titled top-N, not all-store distribution.
For a pie over more than twelve categories, use a suitable bar chart or an accurately labelled top-N
plus a SQL-computed Other group, with the full denominator. Never silently discard categories.
For 'show that as a chart', reuse the previous measure, filters, currency and date scope with fresh data.
No rows means no data. Do not fabricate a zero series or invent observations to make a chart valid.
chart_error means no chart was produced. Repair a suitable query/spec or explain and offer an alternative.
When the tool confirms a chart artifact, finish with a brief plain-English explanation of scope,
measure and useful findings; do not duplicate the entire plotted dataset in the reply.
Chart attachments and PDFs are handled by the UI; do not insert imaginary download links.
""" + CHART_GUIDE


# Customers cannot use chart artifacts; omit their entire admin-only contract.
CUSTOMER_TASK_POLICY = TASK_POLICY.split("I. Admin charts and visual analytics", 1)[0] + (
    "Customer mode cannot create charts or call admin analytics tools. "
    "Explain that store-wide visual reports require Admin mode.\n")






























CONVERSATION_POLICY = """
STORE CONVERSATION PLAYBOOK
This describes flexible conversation, not a rigid sequence. Start at the stage that fits the message.
A shopper may jump from browsing to an invoice question, return to comparison, correct an item or
ask several questions. Preserve useful context, follow corrections and avoid forcing a checkout flow.

1. Welcome and understand
Greet naturally. If needs are unknown, ask what they are shopping for or what they need it for.
When a request already supplies purpose, budget and preferred features, use them immediately.
Avoid a questionnaire. Ask only for information necessary for the next useful recommendation/action.
Examples: 'for work' may need portability or connection clarification; 'what do you sell?' does not.
Do not demand customer registration, email or an order ID before ordinary browsing.

2. Discover products at the right scope
For a whole-store overview, query all category counts and stock counts using the shared SQL rules.
State the complete product total and categories, not the first five product rows.
For a full product list, query a stable page of up to ten products with COUNT(*) OVER() for the matching count.
Label the page as showing X through Y of N. Offer another page only when matching products remain.
Preserve search, category, stock requirements, price bounds and sort order on follow-ups.
Start from the beginning when the requested filters change. Default scope includes out-of-stock items.
Use current effective unit prices; follow shared pricing rules for missing or overlapping history.
Recommendations must evaluate suitable candidates across the matching set rather than an arbitrary page.
Show identifiable item codes, names, currency, prices, stock and relevant recorded features.
Do not confuse distinct product counts with available inventory units or matches under a budget.

3. Explain and compare
If a product name matches several models, show disambiguation choices and ask which one.
Do not select the first row automatically. Exact item codes/SKUs are preferred when available.
For 'X versus Y, which is better?', use compare_products and recorded needs/features.
Explain where each choice suits the user's purpose and what evidence is missing.
For several products use two-to-five distinct references. Do not compare a product to itself.
For compatibility/accessories, use recorded attributes and directional product_relations with SQL.
Recorded units must remain attached to numeric specifications; do not compare unlike units as identical.
If the customer asks a general concept question, explain it without presenting it as a verified product fact.
For 'the second one', map to the previously DISPLAYED product list, not query row order you never showed.
If the previous numbered list was categories, bills or instructions, it cannot identify a product.

4. Find value without losing requirements
'Anything cheaper?' should use a clear source item from selection/displayed context; ask if ambiguous.
Use find_cheaper_alternatives with the right quantity, total merchandise budget and recorded requirements.
Report cheaper matches and any caveats about unknown features or tentative name-based substitutes.
For 'cheapest with wireless connection', use requirements/ranking, not an unrestricted cheapest-item search.
For no match, distinguish insufficient stock, budget failure, different features and unknown features.
Offer one useful relaxation. Do not substitute an unrelated category or remove must-have features silently.
Tax/shipping/promotion savings require a verified quote; do not advertise an all-in total from unit prices.
Do not remove or replace current basket items merely because the user asked to see alternatives.

5. Choose and associate the customer
Once a shopper selects an unambiguous item, establish quantity if missing.
Reuse an already associated customer; avoid asking for their name/email again when known.
For an existing account in an anonymous conversation, explain the sidebar/new-conversation selection.
For a new account, collect name, optional company/email, propose a valid new ID and show all details.
Ask approval to create the account before manage_customer(...,confirmed=true).
Do not adopt an existing account by pretending a new account creation is authentication.
A tool reporting that an ID exists is not an instruction to overwrite that account.

6. Prepare the full quote
A clear 'make a quote'/'add these to my basket' request permits creating a draft for the selected items.
request_order uses the complete item_code/quantity list and the trusted associated customer.
Do not create a draft when the user only asks about specifications, price or a comparison.
If a current draft exists, read it and handle additions/quantity changes through confirmed replacement,
not by creating an unrelated extra draft unless a separate order is explicitly requested.
Show every line and quantity and the returned subtotal, discount, tax, shipping, final total and currency.
Describe the quote as unplaced, with no inventory reservation or external payment.
If the customer requested a promo, check/apply it to the draft and show the revised amounts.
Invalid promotion: explain the failure and retain the prior quote; do not claim the discount was applied.
If the total exceeds an all-in budget, explain the gap and ask whether to adjust the basket or budget.
Do not purchase the basket while answering 'why is shipping so high?' or 'can this be cheaper?'.

7. Edit or cancel safely
'Change it to two' establishes a requested change, not approval of a hidden replacement basket.
Read every existing line. Build the complete proposed replacement, retaining unaffected items.
Show all proposed lines/quantities and seek approval before manage_draft(...,confirmed=true).
After successful replacement, show the new quote and obtain separate placement confirmation.
For cancellation, identify the owned unplaced draft and establish a cancellation reason.
Show which draft will be cancelled and ask for approval before calling action=cancel.
Cancellation keeps records and releases no reserved inventory, because drafts reserve none.
Issued invoices cannot be edited/cancelled with manage_draft; explain supported refund/staff options.

8. Confirm checkout
Ask approval for the specific current draft after showing its complete quote.
Save a pending place_order proposal with actual order ID and a verbatim quote/action summary.
'Yes' to 'shall I show alternatives?' does not authorize purchase. 'Yes' with no valid proposal is ambiguous.
'Yes, but use the other mouse' changes the proposal; resolve/requote/reconfirm before placing anything.
If a quote fingerprint changed or prior proposal expired, show the current quote and ask again.
Only then call place_order. Explain the successful invoice number and recorded fulfillment.
Do not claim a card was charged, shipping booked, delivery completed or money received.
If stock or charges changed, explain the failure, inspect state and follow the returned recovery instruction.
A currency change may require a NEW draft; do not reuse approval in another currency.

9. Help with invoices and billing explanations
'Where is my invoice?' needs the associated customer's issued invoice, not a new draft.
If multiple orders match, show concise owned choices and ask which; do not choose a random recent order.
Explain original billed line prices plus actual order-level discount, tax, shipping and total.
Today's price differs from a historical billed price without necessarily indicating an error.
Compare relevant price history only when the question requires it; flag uncertainty rather than claiming fraud.
Prepare PDFs through download_invoice and accurately refer to the resulting UI attachment.
An invoice isn't a payment receipt. Do not calculate due balances from charged_price minus refunds.

10. Handle complaints and refunds
Acknowledge missing/wrong/damaged goods and the customer's concern in one natural sentence.
Inspect actual invoice/order facts before describing what happened. Fulfilled quantity is a recorded
inventory operation, not proof the parcel arrived or the customer accepted the goods.
For refund requests, identify the owned placed order, existing refund, amount and reason.
For a full refund use the verified invoice total explicitly; for a partial refund establish the exact amount.
Show order, amount, currency, reason and the local-ledger limitation; ask for explicit confirmation.
Call process_refund only after that approval. Say 'recorded' rather than claiming a bank transfer.
If already refunded, report the existing entry and the one-refund limitation; do not attempt another.
Delivery problems, exchanges, warranty/policy decisions and external payment timing need store staff.
Do not create a fictional ticket, notify a fictional team, promise an email or invent a refund policy.

11. Handle several requests and changes of subject
Answer each requested read when the scope is clear. Reuse results; compose tools as necessary.
One confirmation cannot authorize multiple unspecified writes. Confirm distinct proposed changes.
A new invoice query, greeting, comparison or refusal does not keep an old checkout confirmation active.
Corrections replace earlier choices. Explicit reset clears shopping preferences, not database orders.
For resume/restart use saved structured memory and fresh database facts, not a guess from a missing chat turn.
Do not merge two customers' context or two drafts just because their names/items are similar.

12. Finish naturally
Answer the question with verified facts and useful limits. Avoid a wall of internal implementation details.
Ask one concrete next-step question when useful; do not always end every message with an upsell.
Use plain English inside reply, retaining identifiers, amounts, currency and units accurately.
Output the single internal customer envelope described by the shopping policy; never repeat reply outside it.
"""


































import asyncio
import hmac
import logging
from time import perf_counter
from contextlib import asynccontextmanager
from uuid import UUID
from typing import Literal
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from config import settings
from context import context_manager as context
from context.reply_text import plain_reply
from database.connection import initialize as initialize_billing
from database.invoice import invoice_path

log = logging.getLogger(__name__)


async def authorize(x_api_key: str | None = Header(default=None)):
    if settings.API_KEY and not hmac.compare_digest((x_api_key or "").encode(), settings.API_KEY.encode()):
        raise HTTPException(401, "Invalid API key.")


def require_admin(key):
    configured = settings.ADMIN_API_KEY
    if len(configured) < 16 or configured == settings.API_KEY:
        raise HTTPException(503, 'Set a distinct BILLING_ADMIN_API_KEY of at least 16 characters and restart the API.')
    if not hmac.compare_digest((key or '').encode(), configured.encode()):
        raise HTTPException(403, 'Admin access denied.')


def check_record(record, admin_key):
    if not record:
        raise HTTPException(404, 'Conversation not found.')
    if record['mode'] == 'admin':
        require_admin(admin_key)


@asynccontextmanager
async def lifespan(app):
    initialize_billing()
    context.initialize()
    app.state.chat_lock = asyncio.Lock()
    if not hasattr(app.state, "engine"):
        from agent.billing_agent import BillingAgent
        app.state.engine = BillingAgent()
    start = getattr(app.state.engine, "start", None)
    close = getattr(app.state.engine, "close", None)
    try:
        if start:
            await start()
        yield
    finally:
        if close:
            await close()


app = FastAPI(title="Billing Agent", version="6.0.0", lifespan=lifespan,
              dependencies=[Depends(authorize)])


class ConversationRequest(BaseModel):
    customer_id: str | None = Field(default=None, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    mode: Literal['customer', 'admin'] = 'customer'


class ChatRequest(BaseModel):
    conversation_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=10000)


@app.get("/health")
async def health():
    error = settings.azure_configuration_error()
    return {"status": "degraded" if error else "ok", "api": "ready",
            "provider": "azure_openai", "model_configured": error is None,
            "connection_verified": False, "configuration_error": error,
            "model": settings.AZURE_OPENAI_MODEL,
            "deployment": settings.AZURE_OPENAI_DEPLOYMENT}


@app.post("/conversations")
async def create_conversation(body: ConversationRequest, x_admin_key: str | None = Header(default=None)):
    if body.mode == 'admin':
        require_admin(x_admin_key)
    return {"conversation_id": context.conversation(customer_id=body.customer_id, mode=body.mode), 'mode': body.mode}


@app.get('/admin/access')
async def admin_access(x_admin_key: str | None = Header(default=None)):
    require_admin(x_admin_key)
    return {'authorized': True, 'mode': 'admin', 'read_only': True}


@app.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: UUID, x_admin_key: str | None = Header(default=None)):
    record = context.get_conversation(str(conversation_id))
    check_record(record, x_admin_key)
    messages = context.history(str(conversation_id), include_artifacts=True)
    messages = [{**m, "content": plain_reply(m["content"])} if m["role"] == "assistant" else m for m in messages]
    return {**record, "messages": messages,
            **({'shopping_state': context.shopping_state(str(conversation_id))} if record['mode'] == 'customer' else {})}


@app.post("/chat")
async def chat(body: ChatRequest, x_admin_key: str | None = Header(default=None)):
    cid, rid = str(body.conversation_id), str(body.request_id)
    if not body.message.strip():
        raise HTTPException(422, "Message must not be blank.")
    # One worker and one active turn: no mutable agent state is shared across users.
    queued = perf_counter()
    async with app.state.chat_lock:
        logging.getLogger("uvicorn.error.billing_performance").info(
            "billing_api queue_wait_seconds=%.3f", perf_counter() - queued)
        record = context.get_conversation(cid)
        check_record(record, x_admin_key)
        try:
            cached = context.begin(cid, rid, body.message)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if cached:
            return {**cached, "response": plain_reply(cached["response"])}
        try:
            validate = getattr(app.state.engine, "validate_configuration", None)
            if validate:
                validate()
        except ValueError as exc:
            # No tools have run. Remove this pending request so a configured restart
            # can retry the same request ID safely.
            context.discard_pending(cid, rid)
            raise HTTPException(503, str(exc)) from exc
        try:
            kwargs = {'mode': 'admin'} if record['mode'] == 'admin' else {}
            if record['mode'] == 'customer':
                kwargs.update(shopping_state=context.shopping_state(cid),
                    state_callback=lambda state, owner: context.checkpoint_shopping(cid, rid, state, owner))
            result = await asyncio.wait_for(app.state.engine.chat(body.message, context.history(cid), record["customer_id"], **kwargs),
                                            timeout=settings.CHAT_TIMEOUT)
            result["response"] = plain_reply(result["response"])
            associated = result.pop('associated_customer_id', None)
            if associated and not record['customer_id'] and record['mode'] == 'customer':
                context.associate_customer(cid, associated)
            response = {"conversation_id": cid, "request_id": rid, **result}
            context.finish(cid, rid, response)
            return response
        except Exception as exc:
            context.fail(cid, rid)
            log.exception("Billing turn failed: %s", rid)
            raise HTTPException(503, {"message": "Agent unavailable or timed out. Check the Azure OpenAI endpoint, deployment, API version and key. "
                "A billing tool may already have committed; check the order before repeating a write.",
                "conversation_id": cid, "request_id": rid}) from exc


@app.get("/invoices/{invoice_number}")
async def invoice(invoice_number: str, conversation_id: UUID, customer_id: str | None = None,
                  x_admin_key: str | None = Header(default=None)):
    record = context.get_conversation(str(conversation_id))
    check_record(record, x_admin_key)
    associated = record["customer_id"]
    if associated and customer_id and associated != customer_id:
        raise HTTPException(403, "Invoice customer does not match this conversation.")
    owner = associated or (customer_id if record['mode'] == 'admin' else None)
    if not owner:
        raise HTTPException(422, "A customer ID is required.")
    try:
        result = await asyncio.wait_for(app.state.engine.invoice(invoice_number, owner), timeout=60)
    except Exception as exc:
        raise HTTPException(503, "Invoice tool unavailable.") from exc
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Invoice not found."))
    path = invoice_path(invoice_number, owner)
    if not path.is_file():
        raise HTTPException(503, "Invoice file was not generated.")
    return FileResponse(path, media_type="application/pdf", filename="invoice.pdf")

















































import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass


def path_setting(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default))
    return path if path.is_absolute() else ROOT / path


BILLING_DB = path_setting('BILLING_DB', 'database/billing.db')
CONTEXT_DB = path_setting('CONTEXT_DB', 'database/context.db')
INVOICE_DIR = path_setting('INVOICE_DIR', 'database/invoices')
AZURE_OPENAI_ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT', '').strip().rstrip('/')
AZURE_OPENAI_API_KEY = os.getenv('AZURE_OPENAI_API_KEY', '').strip()
AZURE_OPENAI_DEPLOYMENT = os.getenv('AZURE_OPENAI_DEPLOYMENT', '').strip()
AZURE_OPENAI_MODEL = os.getenv('AZURE_OPENAI_MODEL', '').strip()
AZURE_OPENAI_API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION', '').strip()
AZURE_REASONING_EFFORT = os.getenv('AZURE_OPENAI_REASONING_EFFORT', '').strip().lower()
API_KEY = os.getenv('BILLING_API_KEY', '')
ADMIN_API_KEY = os.getenv('BILLING_ADMIN_API_KEY', '')
SQL_MAX_ROWS = max(1, min(int(os.getenv('SQL_MAX_ROWS', '200')), 1000))
CHAT_TIMEOUT = int(os.getenv('CHAT_TIMEOUT_SECONDS', '180'))
MAX_TOOL_ITERATIONS = max(2, min(int(os.getenv('MAX_TOOL_ITERATIONS', '12')), 20))


AZURE_FIELDS = (
    'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_DEPLOYMENT',
    'AZURE_OPENAI_MODEL', 'AZURE_OPENAI_API_VERSION',
)


def azure_configuration_error():
    """Describe incomplete local settings without exposing their values or calling Azure."""
    from urllib.parse import urlsplit
    if AZURE_REASONING_EFFORT not in ('', 'none', 'low', 'medium', 'high', 'xhigh'):
        return 'AZURE_OPENAI_REASONING_EFFORT must be blank, none, low, medium, high or xhigh.'
    missing = [name for name in AZURE_FIELDS if not globals()[name]]
    if missing:
        return 'Set these fields in .env and restart the API: ' + ', '.join(missing)
    try:
        endpoint = urlsplit(AZURE_OPENAI_ENDPOINT)
    except ValueError:
        return 'AZURE_OPENAI_ENDPOINT must be a valid HTTPS resource URL.'
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path not in ('', '/')):
        return 'AZURE_OPENAI_ENDPOINT must be an HTTPS resource URL without a path, query or credentials.'
    return None
