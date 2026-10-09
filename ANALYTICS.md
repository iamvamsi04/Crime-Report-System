# Optional billing analytics tools

This extension adds two read-only tools. All existing source files remain unchanged.

Copy the four new Python files into their matching project folders, then stop the
existing API and launch the extension from the project directory:

```powershell
python app_analytics.py
```

Start Streamlit as usual. Existing Azure settings, chat history, billing operations
and invoice downloads are reused. No additional packages or database migrations
are required. Running the original `python app.py` still exposes only six tools.

## Tools

- `billing_analysis`: invoice totals, discounts, tax, shipping, ledger refunds,
  net billed amounts, current product cost estimates, and top customers/products.
  Filters: `customer_id`, `order_id`, `date_from`, `date_to`; optional `top_n` (1–25).
- `billing_anomalies`: checks total/line/subtotal arithmetic, quantities, refund
  consistency, and prices against available dated history. Flags unusually large
  invoices and suggests reviews of refund causes, margin pressure and inventory.
  Same filters; optional `max_findings` (1–100) and `high_charge_threshold` (default 5000).

Both tools use a read-only SQLite snapshot and parameterized filters. Financial
totals include all selected invoices, independently of the existing SQL tool's
200-row response cap. More than 10,000 matching orders requires a narrower filter.
Dates are inclusive order dates; refund totals therefore refer to refunds on those
orders, not refunds processed during that period (the schema has no refund date).

Drafts are excluded from financial totals. Costs use current item baseprice;
historical purchase costs are unavailable. Margin estimates exclude refunds,
tax, shipping and overhead. Currency is taken from current billing configuration.
Ledger entries do not prove payments occurred. Rules flag review candidates;
suggestions are not guarantees of savings and never execute changes.

Inventory suggestions are suppressed for customer/order-specific filters because
those filters omit other customers' demand. Even a global analysis covers only
products billed in the selected period, and needs a seasonality/demand review.

## Example chat requests

- Show the billing cost breakdown for all invoiced orders.
- Analyze billing for FAKE-CUST-0001.
- Break down invoice costs for order FAKE-ORD-0001.
- Find billing anomalies between 2026-04-01 and 2026-10-08.
- Suggest ways to reduce costs based on refunds, margins and inventory.

## New files

```text
app_analytics.py
agent/analytics_agent.py
mcp_server/server_analytics.py
mcp_server/tools/analytics_tools.py
ANALYTICS.md
```

The alternate server imports the original registrations and adds the two new tools.
The alternate agent inherits the existing Azure validation and invoice method and
uses an extended prompt and workbench for chat. The launcher supplies this agent
to the existing API without editing its source. The original six tools retain
their original implementations.

## Verification

Verified against a temporary copy of the seeded database: all 300 invoice totals,
50 excluded drafts, 60 ledger refunds, filter behavior and read-only connections.
The original seed produced no integrity anomaly flags. Injected total, line,
quantity, refund metadata and historical-price errors were detected.

The real extended MCP server exposed all eight tools and ran both analytics tools
and the original SQL tool. The new launcher served the existing API through an
AutoGen tool/reflection cycle with a deterministic replay model. Live Azure model
responses were not tested. SHA-256 checks confirmed every pre-existing project
file and the original database were unchanged.
