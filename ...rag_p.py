1. Start with the entry point and settings.
   [app.py (line 5)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/app.py:5) starts the FastAPI server through Uvicorn.
   if __name__ == "__main__":
   This means “run this block when someone executes this file directly.” Importing the file from another module does not start another server.
   uvicorn.run("api.main:app", ..., workers=1)
   "api.main:app" identifies the app object inside the API module. workers=1 starts one server worker, matching the current design’s shared agent/workbench and serialized chat execution.
   Streamlit runs separately as the frontend. The MCP tool server is another local process started by the agent.
   [config/settings.py (line 4)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/config/settings.py:4) establishes the project root, loads .env and reads configuration.
   Important details:
   - Path(__file__).resolve().parents[1] locates the project independently of the terminal’s current directory.
   - os.getenv(...) reads an environment variable and optionally supplies a default.
   - path_setting() resolves relative storage paths against the project root.
   - azure_configuration_error() checks required settings and URL format without contacting Azure.
   Current defaults include a 200-row ordinary SQL limit, 180-second chat timeout, 12 tool iterations, a 30-day return window and a cashier refund threshold of 100 USD.
2. Understand how Streamlit submits a question.
   The frontend is [ui/streamlit_app.py (line 347)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/ui/streamlit_app.py:347).
   When a user submits a question, it:
   1. Creates a conversation if necessary.
   2. Generates a unique request_id.
   3. Saves the pending request in st.session_state.
   4. Displays the user’s message.
   5. Calls the API.
   6. Saves the returned answer and attachments.
   7. Reruns the page to display the completed conversation.
   Streamlit normally reruns its script after an interaction. st.session_state carries values across those reruns, including the conversation ID, current login, messages and pending request.
   A pending request retains the same request ID when retried. That gives the backend a way to recognize a duplicate submission.
   The frontend’s cached HTTP client reuses network connections. It does not cache all billing answers.
3. Follow the request into FastAPI.
   The central request handler is [run_chat() (line 274)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/api/main.py:274).
   FastAPI first validates the request using Pydantic models. For example, conversation and request IDs must be valid UUIDs, and messages must satisfy the defined constraints.
   The route decorator:
   @app.post("/chat")
   registers the function below it as the handler for an HTTP POST request to /chat.
   run_chat() then:
   - Rejects blank messages.
   - Checks access to the stored conversation.
   - Looks for an already completed response with the same request ID.
   - Acquires the chat lock.
   - Records the request as pending.
   - Loads the appropriate conversation state.
   - Calls BillingAgent.chat().
   - Validates the returned attachments.
   - Saves the completed response.
   - Returns it to Streamlit.
   await asyncio.wait_for(..., timeout=settings.CHAT_TIMEOUT)
   puts an overall time limit around the agent operation. A timeout does not prove that an earlier database write failed, which is why the error message advises checking the saved order or refund status before repeating a change.
4. Separate customer identity, staff authorization and conversation identity.
   These are different concepts in your code.
   Value	Purpose
   Customer ID	Identifies a customer account
   Password	Proves identity during customer login
   Customer session token	Authenticates subsequent customer requests
   Admin or cashier key	Authorizes staff access
   Conversation ID	Identifies a saved chat
   Request ID	Identifies one submitted question
   
   
   A customer ID or conversation ID alone does not prove ownership.
   [api/main.py (line 71)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/api/main.py:71) resolves the customer from the session token and checks that it matches the conversation’s customer.
   The role also comes from trusted API state. Writing “I am admin” in a chat does not change permissions.
   Staff keys are checked separately. An optional customer ID in staff mode provides account context; it is not a customer login.
5. Understand passwords and sessions precisely.
   The implementation is in [api/customer_auth.py (line 24)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/api/customer_auth.py:24).
   password_hash() uses PBKDF2-SHA256 with a random salt and 600,000 iterations. It stores information shaped like:
   algorithm$iterations$salt$digest
   Login recomputes the digest from the entered password and compares it with the stored digest. The application does not decrypt the stored value to recover a password.
   Hashing happens outside the database write transaction because it is deliberately expensive. The code then rechecks the credentials inside a transaction before completing login.
   Current login behavior:
   - Five failed attempts trigger a three-minute lock.
   - Successful login resets the failure counters.
   - A session lasts 12 hours.
   - Logout deletes that session.
   - Setting a new password revokes the customer’s existing sessions.
   new_session() generates a random token. The UI receives the original token; the database stores its SHA-256 digest.
   Later requests send the token, which is hashed and checked against an unexpired session record. This avoids sending the password with every request.
   The session is an opaque token backed by a database lookup, rather than a JWT containing customer claims. Its expiry is fixed; each chat message does not automatically grant another 12 hours.
   A server restart does not inherently delete persisted sessions. A new browser session still needs access to the original token, and your current UI keeps that token in Streamlit session state.
6. Understand what persists between messages.
   [context/context_manager.py (line 28)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/context/context_manager.py:28) manages a separate conversation database containing:
   Table	Stores
   conversations	Conversation identity, customer association and role
   messages	User/assistant messages, request status and saved responses
   shopping_states	Structured shopping context
   
   
   The main lifecycle functions are:
   Function	Responsibility
   conversation()	Creates or resolves a conversation
   history()	Loads completed conversational messages
   completed_response()	Retrieves an immutable completed request
   begin()	Records a new pending request
   checkpoint_shopping()	Saves trusted progress during execution
   finish()	Saves the completed answer and state
   fail()	Marks a failed request
   
   
   Reusing a completed request ID with the same message returns its saved response. Reusing that ID with different text is rejected.
   This is request deduplication. It is not a general cache that answers every repeated question using old business data.
   Normal model history also excludes large attachment payloads. Those attachments remain available when resuming the conversation.
7. Study BillingAgent.chat() as the main orchestration function.
   The central function is [BillingAgent.chat() (line 812)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/billing_agent.py:812).
   Its execution is:
   Stage	What the code does
   Acquire lock	Protects shared client/workbench state
   Reset the turn	Clears previous charts, tables, receipts, counters and error flags
   Establish scope	Sets the verified role and customer
   Restore context	Loads shopping state or staff/analytics context
   Capture pending proposal	Retains the previous proposal for evaluating this reply
   Build prompt	Combines role instructions, schema and trusted state
   Bound history	Keeps a recent, complete conversational suffix
   Create assistant	Constructs a fresh AutoGen assistant for this turn
   Run	Lets the model answer or request tools
   Process completion	Applies state updates and handles verified attachments/errors
   Return	Produces the API response object
   Clean up	Cancels remaining work, logs metrics and resets failed transports
   
   
   Clearing old attachments matters: a new unrelated question must not redisplay charts from the previous answer.
   Capturing and then expiring pending confirmations matters too. The previous proposal can authorize the current human reply, but it must not remain available indefinitely for an unrelated future “yes.”
8. Understand the constructor around line 893.
   This is the important block:
   agent = SafeBillingAssistant(
       "billing_agent",
       model_client=self._client,
       workbench=workbench,
       model_context=UnboundedChatCompletionContext(
           initial_messages=previous
       ),
       system_message=policy,
       reflect_on_tool_use=True,
       max_tool_iterations=settings.MAX_TOOL_ITERATIONS,
   )
   Each argument has a specific job:
   Argument	Meaning
   "billing_agent"	AutoGen’s name for this assistant
   model_client	The reusable Azure communication client
   workbench	The guarded interface to MCP tools
   model_context	Messages available during this assistant run
   initial_messages	The selected previous conversation messages
   system_message	The active behavioral instructions
   reflect_on_tool_use=True	Allows a model response interpreting tool results
   max_tool_iterations	Bounds the tool-use loop
   
   
   UnboundedChatCompletionContext does not mean the entire saved conversation is sent forever. Your code creates a fresh context each turn and supplies already bounded history.
   It does mean that this context class does not impose another trimming policy on messages added during the current run. Azure’s model context limit still applies.
   reflect_on_tool_use=True helps explain the common two-call pattern:
   Model chooses a tool
       → tool executes
       → model explains the result
   More complex questions can require additional model/tool rounds.
9. Know the supporting classes and helpers in the agent file.
   These are the main classes in [billing_agent.py (line 46)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/billing_agent.py:46):
   Class	Why it exists
   RunCancellationToken	Adapts cancellation behavior around the pinned MCP/AutoGen implementation
   TimedAzureClient	Measures model calls and adjusts request options
   RecordingWorkbench	Checks actions, invokes tools and collects verified outputs
   SafeBillingAssistant	Converts dispatch failures into controlled results
   BillingAgent	Owns lifecycle, transports, locking and chat orchestration
   
   
   RunCancellationToken handles a compatibility case where the MCP adapter registers a coroutine instead of a normal cancellable future. It avoids scheduling or awaiting that operation twice.
   TimedAzureClient.create() measures successful model calls and token usage. Its tool-related options also prevent the Azure error you previously encountered:
   extra["parallel_tool_calls"] = NOT_GIVEN
   NOT_GIVEN means the option is omitted from the request. That matters when a reflection request does not include tools; sending parallel_tool_calls=False still sends the parameter.
   Important helper functions are:
   Helper	Purpose
   parameters()	Starts MCP using the same Python interpreter and project environment
   decode_result()	Converts MCP text content into a usable result dictionary
   change_receipt()	Produces a factual receipt for a successful mutation
   mutating_call()	Identifies operations that change business state
   blocked_action_reply()	Prevents a rejected action from becoming a success claim
   staff_refund_snapshot()	Reads the pending refund and its actual owner
   staff_refund_arguments()	Resolves omitted staff arguments to stored request terms
   staff_confirmation()	Prepares a review bound to the stored refund terms
   
   
   SafeBillingAssistant and the workbench distinguish between a rejected operation, an uncertain write, and a completed write followed by a response-handling failure. Those situations must not all trigger an automatic retry.
10. Understand which prompt is active.
   [runtime_policy() (line 259)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/prompts/runtime_policy.py:259) builds the live system prompt.

   It combines:
- Common behavior.
- SQL, pricing and coverage instructions.
- Business-action rules.
- The verified role.
- The current schema.
- Configured return-window and cashier-limit information.
- Customer shopping memory when applicable.
   Customer browsing instructions ask for category/count tables. Admin inventory instructions ask for individual stock records with 200-row pages.

   Those are model instructions. They are not a Python function matching one exact question and replacing its answer.

   The other seven files in [agent/prompts](C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/prompts) retain earlier/reference prompt definitions and are used by tests. They are not all concatenated into every current Azure request.

   [response_contract.py (line 73)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/response_contract.py:73) validates structured results after tools execute. Its old request-wording classifier functions are inert compatibility functions.

   Authorization code still uses regular expressions to recognize confirmation, negation and supplied coupon codes. That serves a different purpose from routing chart or product questions by keywords.
11. Follow a tool call through the trust boundary.
   [RecordingWorkbench.list_tools() (line 451)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/billing_agent.py:451) gives the model only the tools available to its verified role.

   It also removes internal parameters such as:
execution_mode
execution_customer_id
execution_coupon_codes
confirmed
   The application supplies those values after checking the action.

   [action_policy.py (line 446)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/action_policy.py:446) checks human authorization before dispatch. The MCP wrapper then independently checks role and account scope.

   [mcp_server/access.py (line 18)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/access.py:18) carries trusted scope through a ContextVar, resetting it in finally after the operation.

   The roles currently expose:
Role	Tool access
Customer	10 shopping and account-service tools
Cashier	SQL records, invoice generation and permitted refund actions
Admin	SQL, analytics, anomalies, invoices, refund actions and restricted fulfillment actions


   Admin mode does not grant unrestricted database editing through chat.

   Although independent reads can be requested in one model batch, _dispatch_lock preserves their execution/result order. The speed benefit comes primarily from fewer model round trips. Writes and invoice generation must be requested separately.
12. Know all 12 tools and their responsibilities.
   [mcp_server/server.py (line 10)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/server.py:10) registers these functions with FastMCP:
Tool	Responsibility
execute_sql_query	Flexible, permitted read-only queries and optional admin charts
request_order	Creates an unplaced draft from a requested basket
place_order	Finalizes a confirmed draft and issues an invoice
apply_promotion	Validates or applies the exact customer-supplied coupon
process_refund	Quotes, requests, approves and tracks supported refund workflows
download_invoice	Prepares invoice, tax or proforma documents and permitted corrections
billing_analysis	Returns billing summaries, trends and comprehensive store reports
billing_anomalies	Flags deterministic inconsistencies and review opportunities
manage_customer	Updates permitted customer and billing details
manage_draft	Handles draft changes, cancellation, credit allocation and supported service actions
compare_products	Compares recorded product prices, stock, features and requirements
find_cheaper_alternatives	Searches qualifying cheaper products across the matching scope


   These are 12 functions exposed to the assistant, not 12 separate agents.

   MCP is the protocol connecting the assistant to these functions. Your MCP server uses local standard input/output, rather than exposing the billing tools as a separate public web API.
13. Understand how SQL access is restricted.
   [execute_sql_query() (line 120)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/tools/database.py:120) accepts reads such as SELECT, WITH and permitted schema inspection.

   Its protections include:
- SQLite’s authorizer.
- query_only.
- Allowed tables and functions.
- Bound parameters.
- Query size and column limits.
- An approximately two-second SQLite execution deadline.
- Result-row limits.
conn.execute(
    "SELECT * FROM orders WHERE customer_id=?",
    (customer_id,),
)
   The ? keeps a supplied value separate from SQL syntax.

   The trailing comma in (customer_id,) creates a one-element tuple. Without it, parentheses alone do not create a tuple.

   Customers do not query the original database connection. [customer_query_connection() (line 57)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/access.py:57) builds an isolated in-memory database containing public product data and only permitted private records.

   That prevents joins or CTEs from bypassing a superficial ownership filter. Procurement prices, authentication tables and customer coupon enumeration are excluded.

   Ordinary reads default to 200 returned rows; chart reads allow up to 1,000. Fetching one extra row detects truncation. A query containing LIMIT 10 can still have more matching products even when its returned result is not marked truncated.

   [product_search.py (line 20)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/database/product_search.py:20) normalizes product text using Unicode normalization, case folding, separator handling and common plural forms. That makes a search such as “power banks” match power_banks without changing stored product identifiers.
14. Understand database transactions and financial calculations.
   [transaction() (line 22)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/database/connection.py:22) follows this pattern:
begin transaction
perform related changes
append audit information
commit
   On an exception, it rolls back and always closes the connection.

   BEGIN IMMEDIATE acquires SQLite’s write reservation before the operation makes related changes. Stock deduction, invoice lines and order status must succeed together.

   Foreign keys protect relationships. CHECK constraints restrict values and lifecycle states. UNIQUE constraints prevent prohibited duplicates. Append-only triggers protect audit and store-credit history.

   Financial helpers are in [billing_tools.py (line 32)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/tools/billing_tools.py:32).
Decimal(str(value))
   converts through a decimal string, avoiding direct conversion of a binary floating-point representation.
value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
   rounds to cents using the selected financial rounding rule.

   The main calculation is:
discount = applicable coupon discount, capped at subtotal
tax = (subtotal − discount) × tax rate
invoice total = subtotal − discount + tax + shipping
   SQLite’s DECIMAL declaration does not itself provide strict fixed-point storage. Python’s financial helpers perform the controlled decimal calculations; arbitrary SQL aggregates still require appropriate care.

   price_for() selects a historical price active on the relevant date. No matching history falls back to the selling price. Overlapping history is rejected rather than choosing an arbitrary row.
15. Explain the order, coupon and refund lifecycles.
   [request_order() (line 123)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/tools/billing_tools.py:123) validates products and quantities, combines repeated item quantities and creates a draft.

   A draft is an estimate. It does not reserve stock or represent a payment.

   Reusing the same order ID with the same basket is idempotent; using that ID with different basket data is rejected.

   [place_order() (line 228)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/tools/billing_tools.py:228) checks the confirmed draft, recalculates its terms, conditionally deducts stock and writes historical invoice lines.

   A conditional stock update checks that enough quantity remains. If the draft terms changed or a later step fails, the transaction rolls back the earlier changes.

   The lifecycle field is:
draft → placed
draft → cancelled
   Supported placed-order cancellation has its own service rules. Invoice presence and shipping state provide additional facts; they are not substitutes for the order lifecycle.

   Coupon enforcement exists at several levels:
- [coupon_policy.py (line 43)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/coupon_policy.py:43) derives allowed codes from customer messages.
- The workbench checks the customer requested that exact application.
- The MCP wrapper checks the supplied-code list.
- The billing tool checks expiry and prevents stacking or replacement with another code.
- Placement rechecks coupon validity.
   Validation previews a coupon without applying it.

   [process_refund() (line 314)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/tools/billing_tools.py:314) follows this sequence:
Read/quote
    → customer confirms exact terms
    → pending customer request
    → eligible staff approval
    → locally recorded approved refund
   Cashiers and admins cannot create an arbitrary staff refund without a pending customer request.

   Approval checks the stored amount, reason, currency, destination and items. Both request and approval must satisfy the return window. Cashiers must also satisfy their threshold and currency restriction.

   Physical returns require confirmed receipt or a documented admin waiver. Duplicate and top-up refunds are blocked.

   Item-return calculations use billed quantities and the actual allocated discount/tax. Return shipping is excluded.

   Approval records the local refund result. It does not call a payment provider or automatically restock physically returned goods.
16. Understand shopping memory and confirmation fingerprints.
   [ShoppingTracker (line 153)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/context/shopping_state.py:153) maintains structured state including needs, budget, preferred features, observed products, selections, the current draft, pagination and pending confirmation.

   Two sources update it:
Source	What it can establish
Successful tool results	Actual products, order facts and completed changes
Validated model patch	Permitted preferences, references and proposed next actions


   The model cannot invent arbitrary state fields. Pydantic models use extra='forbid' and impose type, size and value limits.

   Product references must refer to observed products. This supports questions such as “choose the second one” without treating every numbered list as a product list.

   [order_snapshot() (line 111)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/context/shopping_state.py:111) reads the relevant order facts and calculates a SHA-256 fingerprint.

   That fingerprint detects whether the proposed terms changed. It is neither encryption nor a password hash.

   A confirmation is bound to the action, order, exact arguments and current fingerprint. “Yes, but change the quantity” requires a revised proposal.

   Successful tool observations are checkpointed before the final model response. A later model failure therefore does not erase an already completed order change.
17. Follow chart, table and invoice output separately from prose.
   A chart follows this path:
Model selects query and chart specification
    → SQL returns actual rows
    → backend validates the chart
    → workbench attaches it
    → API returns the artifact
    → Streamlit renders it with Altair
   [prepare_chart() (line 112)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/charts.py:112) checks returned column names, numeric values, dates, grouping, currency and the selected chart’s required shape.

   The current 15 types are bar, grouped bar, horizontal bar, stacked bar, normalized bar, line, pie, scatter, bubble, histogram, box plot, heatmap, range, combo and timeline.

   There is no fixed chart-count cap per response. Each chart still needs valid, complete data and must satisfy its resource limits. For example, a range needs valid lower/upper bounds, while a histogram needs raw observations.

   Month values such as 2026-04 are accepted as period labels and normalized appropriately. Currency is metadata rather than a constant USD axis.

   [billing_analysis() (line 151)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/tools/analytics_tools.py:151) provides validated summaries and trends. [store_overview() (line 49)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/mcp_server/reporting.py:49) provides comprehensive named datasets and optional chart modes.

   Order totals are aggregated once per order. Product revenue uses invoice lines. Joining order totals directly to multiple lines and summing them would multiply the totals, which the reporting code avoids.

   billing_anomalies applies deterministic checks. A finding is a review candidate, rather than a fraud verdict.

   [ui/charts.py (line 193)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/ui/charts.py:193) translates validated artifacts into Altair charts. It does not ask Azure to draw an image.

   Admin chart streaming uses newline-delimited JSON events. It can display completed charts before the final answer. It is not token-by-token model streaming or SSE.

   Tables are also separate artifacts. [table_presentation.py (line 28)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/table_presentation.py:28) converts verified tool fields into tables without recalculating financial values.

   [ui/tables.py (line 7)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/ui/tables.py:7) hides SKU/item-code columns from displayed tables. Those identifiers remain internally available; this is not global removal from API data, state or PDFs.

   [database/invoice.py (line 16)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/database/invoice.py:16) uses ReportLab to generate PDFs from verified records. Issued documents use billed lines and saved contact information. Proforma documents use draft estimates.
18. Know why reply parsing and prompt files remain separate.
   [reply_policy.py](C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/prompts/reply_policy.py) contains writing instructions.

   [context/reply_text.py (line 32)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/context/reply_text.py:32) contains executable response parsing.

   A customer response can internally look like:
{
  "reply": "Your refund request is pending approval.",
  "shopping_update": {
    "pending_confirmation": null
  }
}
   reply_envelope() locates the structured envelope. plain_reply() extracts the visible response and suppresses internal fragments, preventing a plain-English answer followed by a duplicate JSON answer.

   split_reply_tables() converts suitable explanatory Markdown tables into native UI tables. Live business tables normally come from actual tool rows.
19. Know the database groups and generator’s role.
   The billing schema declares 22 application tables, separate from the three conversation tables.
Group	Tables
Products and pricing	items, product_details, product_attributes, product_relations, price_history
Customer identity	customers, customer_billing_details, customer_credentials, customer_sessions
Orders and invoices	orders, order_requests, order_fulfilled, invoice_lines, order_details, order_service, invoice_revisions
Refunds and local credit	refund_requests, refund_details, store_credit_ledger
Configuration and history	billing_config, promotions, billing_audit


   There are no payment or credit-note tables. Local store credit records grants and redemptions without payment processing.

   [database/migrations.py (line 14)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/database/migrations.py:14) upgrades older structures, removes retired structures and introduces lifecycle/expiry fields.

   [generate_seed.py (line 321)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/generate_seed.py:321) creates related records and audits their relationships, arithmetic and stock.

   Its password stage uses the same password-hashing helper as authentication. Hashing runs outside database write locks, and prepared credentials are committed in batches. Existing credentials are preserved, and interrupted password generation can resume. It does not create a login CSV.
20. Explain performance and the Python patterns behind the lines.
   [model_history.py (line 4)](/C:/Users/vamsi/Documents/Codex/2026-10-05/bu/outputs/version_7_latest/agent/model_history.py:4) defaults to four complete recent turns and an 8,000-byte history budget. The latest turn stays intact even when larger.

   That is a transcript budget, not an 8,000-token total request limit. Instructions, tool schemas, the current question and current tool results also contribute tokens.

   Other efficiency measures include reused Azure connections, a warm MCP process, cached tool schemas, compact JSON and keeping complete report rows outside the model transcript.

   The logs separate queue time, model time, tool time and total turn time. model_calls counts successful completions, which explains why a failed Azure request can leave it at zero.

   The current locks protect shared state by serializing active turns. They can therefore introduce queueing under multiple simultaneous users. async improves how waiting is handled; it does not make Azure inference itself instantaneous.

   These Python patterns appear throughout the code:
Pattern	How to explain it
async def / await	Allows cooperative waiting for network or MCP operations
asyncio.Lock	Prevents concurrent modification of shared state
with / async with	Manages acquisition and cleanup around an operation
try / except / finally	Handles errors and guarantees cleanup
raise	Stops normal execution by propagating an error
raise ... from exc	Preserves the underlying cause of an error
@decorator	Wraps or registers the function below it
*args / **kwargs	Collects or forwards positional/named arguments
deepcopy()	Prevents nested mutable data from being shared accidentally
getattr(obj, name, default)	Reads an optional attribute safely
ContextVar	Carries execution-specific scope without ordinary shared-global leakage
default_factory=list	Creates a fresh list for each model instance
exclude_unset=True	Distinguishes omitted fields from explicitly supplied values
None	Represents absence; its meaning depends on the field
__init__.py	Marks or initializes a Python package


If you are asked to describe the whole project verbally, you can say:
The Streamlit frontend sends authenticated requests to FastAPI. FastAPI manages conversation identity, retries and persistence, then calls a shared BillingAgent. Azure OpenAI interprets the conversation and selects MCP tools. The workbench checks human authorization and supplies trusted account scope. The tools enforce business rules and perform transactional database operations. Verified results return as plain English, tables, charts or invoice attachments, with structured shopping state saved for the next turn.

The distinction to keep clear in technical questions is what the model proposes, what Python authorizes, and what the database actually commits.
