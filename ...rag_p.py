"""Read-only billing insights. Uses stored invoice amounts and current cost estimates."""
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
import sqlite3

from database.connection import connect

ZERO = Decimal("0")
CENT = Decimal(".01")
LIMIT = 10000


def dec(value):
    number = Decimal(str(value if value is not None else 0))
    if not number.is_finite():
        raise ValueError("Non-finite monetary value encountered.")
    return number


def cash(value):
    return str(dec(value).quantize(CENT, rounding=ROUND_HALF_UP))


def result(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except (ValueError, InvalidOperation) as exc:
            return {"ok": False, "error": str(exc)}
        except sqlite3.Error:
            return {"ok": False, "error": "Unable to read the billing database."}
    return wrapped


def filters(customer_id, order_id, date_from, date_to):
    clauses, values = ["NOT EXISTS (SELECT 1 FROM order_details d WHERE d.order_id=o.order_id AND d.status='cancelled')"], []
    for name, value in (("customer_id", customer_id), ("order_id", order_id)):
        if value is not None:
            if not isinstance(value, str) or not value.strip() or len(value) > 50:
                raise ValueError(f"{name} must contain 1-50 characters.")
            clauses.append(f"o.{name}=?")
            values.append(value)
    for name, value, operator in (("date_from", date_from, ">="), ("date_to", date_to, "<=")):
        if value is not None:
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError(f"{name} must use YYYY-MM-DD.") from None
            clauses.append(f"o.order_date{operator}?")
            values.append(value)
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must be on or before date_to.")
    return " AND ".join(clauses) or "1=1", values


@contextmanager
def snapshot(customer_id, order_id, date_from, date_to):
    where, args = filters(customer_id, order_id, date_from, date_to)
    conn = connect(readonly=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        orders = [dict(r) for r in conn.execute(
            f"SELECT o.*,c.name AS customer_name FROM orders o LEFT JOIN customers c "
            f"ON c.customer_id=o.customer_id WHERE {where} ORDER BY o.order_date,o.order_id LIMIT ?",
            [*args, LIMIT + 1])]
        if len(orders) > LIMIT:
            raise ValueError("More than 10000 orders match. Narrow the date range or customer filter.")
        related = {}
        for table in ("invoice_lines", "order_requests", "order_fulfilled"):
            related[table] = [dict(r) for r in conn.execute(
                f"SELECT x.* FROM {table} x JOIN orders o ON o.order_id=x.order_id WHERE {where}", args)]
        # Load only products/history records referenced by this selection.
        item_filter = (
            "SELECT item_code FROM invoice_lines WHERE order_id IN (SELECT o.order_id FROM orders o WHERE " + where + ") "
            "UNION SELECT item_code FROM order_requests WHERE order_id IN (SELECT o.order_id FROM orders o WHERE " + where + ") "
            "UNION SELECT item_code FROM order_fulfilled WHERE order_id IN (SELECT o.order_id FROM orders o WHERE " + where + ")"
        )
        items = {r["item_code"]: dict(r) for r in conn.execute(
            f"SELECT * FROM items WHERE item_code IN ({item_filter})", args * 3)}
        histories = defaultdict(list)
        for row in conn.execute(f"SELECT * FROM price_history WHERE item_code IN ({item_filter})", args * 3):
            histories[row["item_code"]].append(dict(row))
        config = dict(conn.execute("SELECT key,value FROM billing_config"))
        currencies = {r[0] for r in conn.execute(
            f"SELECT DISTINCT COALESCE(d.currency,?) FROM orders o LEFT JOIN order_details d "
            f"ON d.order_id=o.order_id WHERE o.order_invoice IS NOT NULL AND {where}",
            [config.get('currency', 'USD'), *args])}
        if len(currencies) > 1:
            raise ValueError('Selection spans multiple currencies. Narrow the selection or query separate currency groups.')
        if currencies:
            config['currency'] = next(iter(currencies))
        yield orders, related, items, histories, config
    finally:
        conn.close()


def group(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["order_id"]].append(row)
    return grouped


def invoiced(order):
    return bool(order["order_invoice"])


def summary(orders):
    fields = ("subtotal", "discount_applied", "tax_amount", "shipping_amount", "charged_price", "refund_amount")
    amounts = {field: sum((dec(order[field]) for order in orders), ZERO) for field in fields}
    return {**{field: cash(value) for field, value in amounts.items()},
            "net_billed_after_ledger_refunds": cash(amounts["charged_price"] - amounts["refund_amount"])}


def costing(orders, lines, items):
    selected = {o["order_id"] for o in orders}
    products = {}
    missing = set()
    for line in lines:
        if line["order_id"] not in selected:
            continue
        code = line["item_code"]
        item = items.get(code)
        if item is None:
            missing.add(code)
            continue
        p = products.setdefault(code, {"item_code": code, "name": item["name"],
            "units_billed": 0, "line_revenue": ZERO, "estimated_product_cost": ZERO,
            "current_unit_cost": cash(item["baseprice"]), "available_quantity": item["availablequantity"]})
        p["units_billed"] += line["qty_billed"]
        p["line_revenue"] += dec(line["line_total"])
        p["estimated_product_cost"] += dec(item["baseprice"]) * line["qty_billed"]
    return products, sorted(missing)


@result
def billing_analysis(customer_id: str | None = None, order_id: str | None = None,
                     date_from: str | None = None, date_to: str | None = None,
                     top_n: int = 10) -> dict:
    """Analyze invoices and break down revenue, discounts, tax, shipping, ledger refunds and estimated costs.

    Optional filters intersect; dates are inclusive order dates (YYYY-MM-DD).
    Drafts are counted but excluded from financial totals. Costs use CURRENT item
    baseprice, not historical cost. This is a billing ledger, not payment settlement.
    Use for billing analytics or cost breakdown questions; never modifies records.
    """
    if type(top_n) is not int or not 1 <= top_n <= 25:
        raise ValueError("top_n must be an integer from 1 to 25.")
    with snapshot(customer_id, order_id, date_from, date_to) as (orders, related, items, history, config):
        placed = [o for o in orders if invoiced(o)]
        products, missing = costing(placed, related["invoice_lines"], items)
        estimated = sum((p["estimated_product_cost"] for p in products.values()), ZERO)
        amounts = summary(placed)
        customers = defaultdict(list)
        for order in placed:
            customers[order["customer_id"]].append(order)
        customer_rows = [{"customer_id": cid, "name": group[0]["customer_name"],
                          "invoice_count": len(group), **summary(group)} for cid, group in customers.items()]
        customer_rows.sort(key=lambda row: dec(row["charged_price"]), reverse=True)
        product_rows = []
        for product in sorted(products.values(), key=lambda p: p["line_revenue"], reverse=True)[:top_n]:
            product_rows.append({**product, "line_revenue": cash(product["line_revenue"]),
                                 "estimated_product_cost": cash(product["estimated_product_cost"])})
        return {"filters": {"customer_id": customer_id, "order_id": order_id, "date_from": date_from, "date_to": date_to},
            "currency": config.get("currency", "USD"), "matched_orders": len(orders),
            "invoice_count": len(placed), "draft_count": len(orders)-len(placed),
            "refunded_invoice_count": sum(dec(o["refund_amount"]) > 0 for o in placed),
            "totals": amounts, "average_invoice_total": cash(dec(amounts["charged_price"])/len(placed)) if placed else "0.00",
            "estimated_product_cost": cash(estimated) if not missing else None,
            "estimated_merchandise_margin_before_refunds": cash(dec(amounts["subtotal"])-dec(amounts["discount_applied"])-estimated) if not missing else None,
            "missing_product_items": missing,
            "top_customers": customer_rows[:top_n], "customers_truncated": len(customer_rows)>top_n,
            "top_products": product_rows, "products_truncated": len(products)>top_n,
            "notes": ["All matching invoices contribute to totals; top lists alone are limited.",
                "Cost and margin estimates use current baseprice. Historical purchase cost and operating expenses are unavailable.",
                "Product line revenue is before order-level discounts, tax, shipping and refunds.",
                "Refunds are ledger entries. Net billed does not prove money was collected or returned.",
                "Currency uses invoice snapshots where available; legacy invoices use the current configuration.",
                "These are invoice face-value analytics. Payments and credit notes are not tracked; these totals are not outstanding balances or proof of payment."]}


@result
def billing_anomalies(customer_id: str | None = None, order_id: str | None = None,
                      date_from: str | None = None, date_to: str | None = None,
                      max_findings: int = 25, high_charge_threshold: str = "5000.00") -> dict:
    """Detect inconsistent invoice totals, quantities, refunds and historical prices; suggest cost reviews.

    Read-only rules, not fraud predictions. Returns evidence and review suggestions,
    including refund exposure, current-cost margin pressure and stocked products.
    high_charge_threshold is in the configured currency. Dates filter order_date.
    """
    if type(max_findings) is not int or not 1 <= max_findings <= 100:
        raise ValueError("max_findings must be an integer from 1 to 100.")
    threshold = dec(high_charge_threshold)
    if threshold <= 0:
        raise ValueError("high_charge_threshold must be positive.")
    findings = []
    def flag(rule, order, evidence, severity="warning"):
        findings.append({"rule": rule, "severity": severity, "order_id": order["order_id"], "evidence": evidence})
    with snapshot(customer_id, order_id, date_from, date_to) as (orders, related, items, histories, config):
        billed, fulfilled, requested = (group(related[name]) for name in ("invoice_lines", "order_fulfilled", "order_requests"))
        for order in orders:
            oid = order["order_id"]
            lines, shipped, demand = billed[oid], fulfilled[oid], requested[oid]
            expected = dec(order["subtotal"])-dec(order["discount_applied"])+dec(order["tax_amount"])+dec(order["shipping_amount"])
            if cash(expected) != cash(order["charged_price"]):
                flag("TOTAL_MISMATCH", order, {"expected": cash(expected), "stored": cash(order["charged_price"])}, "error")
            if dec(order["discount_applied"]) > dec(order["subtotal"]):
                flag("EXCESS_DISCOUNT", order, {"discount": cash(order["discount_applied"]), "subtotal": cash(order["subtotal"])}, "error")
            refund = dec(order["refund_amount"])
            if order["refund_amount"] is not None:
                if refund <= 0 or refund > dec(order["charged_price"]) or not invoiced(order):
                    flag("INVALID_REFUND", order, {"refund_amount": cash(refund), "charged_price": cash(order["charged_price"])}, "error")
                if not order["refund_status"] or not order["refund_reason"]:
                    flag("INCOMPLETE_REFUND", order, "Refund has no status or reason.", "error")
            elif order["refund_status"] or order["refund_reason"]:
                flag("INCOMPLETE_REFUND", order, "Refund metadata exists without an amount.", "error")
            if not invoiced(order):
                if lines or shipped:
                    flag("DRAFT_HAS_BILLING_RECORDS", order, "Invoice number missing despite invoice/fulfillment lines.", "error")
                continue
            if not lines:
                flag("INVOICE_WITHOUT_LINES", order, "Invoice has no persisted invoice lines.", "error")
            line_sum = sum((dec(line["line_total"]) for line in lines), ZERO)
            if cash(line_sum) != cash(order["subtotal"]):
                flag("SUBTOTAL_MISMATCH", order, {"line_sum": cash(line_sum), "subtotal": cash(order["subtotal"])}, "error")
            def quantities(records, field):
                counts = Counter()
                for row in records:
                    counts[row["item_code"]] += row[field]
                return dict(counts)
            if not (quantities(lines, "qty_billed") == quantities(shipped, "quantity") == quantities(demand, "quantity")):
                flag("QUANTITY_MISMATCH", order, {"billed": quantities(lines, "qty_billed"),
                     "fulfilled": quantities(shipped, "quantity"), "requested": quantities(demand, "quantity")}, "error")
            for line in lines:
                evidence = {"item_code": line["item_code"], "invoice_line_id": line["id"]}
                if line["qty_billed"] <= 0 or dec(line["unit_price_billed"]) < 0 or dec(line["line_total"]) < 0:
                    flag("INVALID_INVOICE_LINE", order, evidence, "error")
                if cash(dec(line["unit_price_billed"])*line["qty_billed"]) != cash(line["line_total"]):
                    flag("LINE_TOTAL_MISMATCH", order, evidence, "error")
                history = [h for h in histories[line["item_code"]] if h["effective_from"] <= order["order_date"]
                           and (not h["effective_to"] or h["effective_to"] >= order["order_date"])]
                if len(history) > 1:
                    flag("OVERLAPPING_PRICE_HISTORY", order, evidence, "error")
                elif history and cash(history[0]["unit_price"]) != cash(line["unit_price_billed"]):
                    flag("HISTORICAL_PRICE_MISMATCH", order, {**evidence,
                         "history_price": cash(history[0]["unit_price"]), "billed_price": cash(line["unit_price_billed"])})
                if line["item_code"] not in items:
                    flag("MISSING_PRODUCT_ITEM", order, evidence, "error")
            if dec(order["charged_price"]) > threshold:
                flag("HIGH_INVOICE_VALUE", order, {"total": cash(order["charged_price"]), "threshold": cash(threshold)}, "review")
        placed = [o for o in orders if invoiced(o)]
        products, missing = costing(placed, related["invoice_lines"], items)
        recommendations = []
        refunds = [o for o in placed if dec(o["refund_amount"]) > 0]
        if placed and refunds:
            rate = Decimal(len(refunds))*100/len(placed)
            if rate >= 15:
                recommendations.append({"action": "Review common refund reasons and product quality before further purchasing.",
                    "evidence": {"refunded_invoices": len(refunds), "invoices": len(placed), "refund_rate_percent": cash(rate),
                                 "ledger_refund_exposure": cash(sum((dec(o["refund_amount"]) for o in refunds), ZERO))},
                    "basis": "15 percent or more of selected invoices have a ledger refund; this is exposure, not guaranteed savings."})
        low_margin = []
        for order in placed:
            lines = billed[order["order_id"]]
            if not lines or any(line["item_code"] not in items for line in lines):
                continue
            cost = sum((dec(items[l["item_code"]]["baseprice"])*l["qty_billed"] for l in lines), ZERO)
            net = dec(order["subtotal"])-dec(order["discount_applied"])
            if net-cost < net*Decimal("0.10"):
                low_margin.append(order["order_id"])
        if low_margin:
            recommendations.append({"action": "Review discounts and supplier costs on invoices with less than 10 percent estimated merchandise margin.",
                "affected_invoice_count": len(low_margin), "example_order_ids": low_margin[:10],
                "basis": "Estimate uses current baseprice and excludes refunds, tax, shipping and operating costs."})
        # Customer/order filters describe only part of product demand: do not infer global overstock from them.
        if not customer_id and not order_id:
            stock = [p for p in products.values() if p["available_quantity"] >= 50
                     and p["available_quantity"] > 4*p["units_billed"]]
            stock.sort(key=lambda p: dec(p["current_unit_cost"])*p["available_quantity"], reverse=True)
            if stock:
                recommendations.append({"action": "Review replenishment for stock large relative to billed units in the selected period.",
                    "affected_product_count": len(stock), "examples": [{"item_code": p["item_code"],
                        "on_hand": p["available_quantity"], "units_billed_in_period": p["units_billed"],
                        "inventory_value_at_current_cost": cash(dec(p["current_unit_cost"])*p["available_quantity"])} for p in stock[:10]],
                    "basis": "At least 50 on hand and over four times period billed units. Review seasonality and demand outside the selected period; inventory value is not savings."})
        findings.sort(key=lambda f: ({"error": 0, "warning": 1, "review": 2}[f["severity"]], f["order_id"], f["rule"]))
        return {"currency": config.get("currency", "USD"), "matched_orders": len(orders),
            "filters": {"customer_id": customer_id, "order_id": order_id, "date_from": date_from, "date_to": date_to},
            "high_charge_threshold": cash(threshold), "finding_count": len(findings),
            "counts_by_rule": dict(Counter(f["rule"] for f in findings)), "findings": findings[:max_findings],
            "findings_truncated": len(findings)>max_findings, "optimization_suggestions": recommendations,
            "notes": ["Rules identify review candidates; they do not establish fraud or guaranteed savings.",
                "Drafts are checked for total/refund/record consistency but excluded from invoice-based recommendations.",
                "Price mismatches compare against currently stored dated price history; missing history is not replaced with today's selling price.",
                "Historical tax rates, shipping costs, purchase costs and promotion versions are unavailable, so they are not inferred.",
                "Inventory suggestions cover products billed in the selected period, not the entire products.",
                "No changes or refunds are executed by this tool."]}




































"""All mutations are serialized by BEGIN IMMEDIATE and use Decimal totals."""
import functools
import re
import sqlite3
import inspect
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
from database.connection import transaction, connect, audit_context


class BillingError(ValueError):
    pass


def tool_result(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        token = audit_context.set({'action': fn.__name__,
            'arguments': dict(inspect.signature(fn).bind(*args, **kwargs).arguments)})
        try:
            return {"ok": True, **fn(*args, **kwargs)}
        except (BillingError, InvalidOperation) as exc:
            return {"ok": False, "error": str(exc)}
        except sqlite3.Error:
            return {"ok": False, "error": "Database operation failed; no transaction was committed."}
        finally:
            audit_context.reset(token)
    return wrapped


def money(value):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or value > Decimal("99999999.99"):
        raise BillingError("Money must be finite, nonnegative and fit DECIMAL(10,2).")
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", value):
        raise BillingError(f"Invalid {label}; use 1-50 letters, digits, underscores or hyphens.")
    return value


def order_row(conn, order_id, customer_id):
    row = conn.execute("SELECT * FROM orders WHERE order_id=? AND customer_id=?",
                       (order_id, customer_id)).fetchone()
    if row is None:
        raise BillingError("Order not found for this customer.")
    meta = conn.execute("SELECT status FROM order_details WHERE order_id=?", (order_id,)).fetchone()
    if meta and meta['status'] == 'cancelled':
        raise BillingError("This draft was cancelled. Create a new order.")
    return row


def is_placed(conn, order):
    return bool(order["order_invoice"] or conn.execute(
        "SELECT 1 FROM order_fulfilled WHERE order_id=?", (order["order_id"],)).fetchone())


def lines_for(conn, order_id):
    return conn.execute("SELECT r.item_code,r.quantity,i.sellingprice,i.availablequantity "
                        "FROM order_requests r JOIN items i ON i.item_code=r.item_code "
                        "WHERE r.order_id=? ORDER BY r.item_code", (order_id,)).fetchall()


def price_for(conn, item, date):
    rows = conn.execute("SELECT unit_price FROM price_history WHERE item_code=? "
                        "AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)",
                        (item["item_code"], date, date)).fetchall()
    if len(rows) > 1:
        raise BillingError("Overlapping price history requires an operator correction.")
    return money(rows[0][0] if rows else item["sellingprice"])


def totals(conn, subtotal, promo_code=None):
    config = dict(conn.execute("SELECT key,value FROM billing_config"))
    tax_rate = Decimal(config.get("tax_rate", "0"))
    if not tax_rate.is_finite() or not 0 <= tax_rate <= 100:
        raise BillingError("tax_rate must be a percentage between 0 and 100.")
    discount = Decimal(0)
    if promo_code:
        promo = conn.execute("SELECT * FROM promotions WHERE promo_code=?", (promo_code,)).fetchone()
        if not promo:
            raise BillingError("Promotion not found.")
        value = money(promo["discount_value"])
        if promo["discount_type"] == "percent" and value <= 100:
            discount = money(subtotal * value / 100)
        elif promo["discount_type"] == "flat":
            discount = value
        else:
            raise BillingError("Invalid promotion definition.")
    discount = min(discount, subtotal)
    tax = money((subtotal - discount) * tax_rate / 100)
    shipping = money(config.get("shipping_amount", "0"))
    return dict(subtotal=str(money(subtotal)), discount_applied=str(money(discount)),
                tax_amount=str(tax), shipping_amount=str(shipping),
                charged_price=str(money(subtotal - discount + tax + shipping)))


def update_totals(conn, order_id, values):
    conn.execute("UPDATE orders SET subtotal=:subtotal,discount_applied=:discount_applied,"
                 "tax_amount=:tax_amount,shipping_amount=:shipping_amount,charged_price=:charged_price "
                 "WHERE order_id=:order_id", {**values, "order_id": order_id})


@tool_result
def request_order(customer_id: str, items: list[dict], order_id: str | None = None) -> dict:
    """Create a draft. items contains item_code and integer quantity. Reuse order_id on retries."""
    identifier(customer_id, "customer ID")
    order_id = identifier(order_id or "ORD-" + uuid4().hex[:20], "order ID")
    audit_context.get()['arguments']['order_id'] = order_id
    if not isinstance(items, list) or not items or len(items) > 100:
        raise BillingError("Provide between 1 and 100 items.")
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise BillingError("Each item must contain item_code and quantity.")
        code = identifier(item.get("item_code"), "item code")
        qty = item.get("quantity")
        if type(qty) is not int or not 1 <= qty <= 100000:
            raise BillingError("Quantity must be an integer between 1 and 100000.")
        quantities[code] = quantities.get(code, 0) + qty
        if quantities[code] > 100000:
            raise BillingError("Combined quantity exceeds 100000.")
    with transaction() as conn:
        if not conn.execute("SELECT 1 FROM customers WHERE customer_id=?", (customer_id,)).fetchone():
            raise BillingError("Customer not found.")
        existing = conn.execute("SELECT * FROM orders WHERE order_id=?", (order_id,)).fetchone()
        if existing:
            order_row(conn, order_id, customer_id)
            previous = dict(conn.execute("SELECT item_code,quantity FROM order_requests WHERE order_id=?", (order_id,)))
            if existing["customer_id"] != customer_id or previous != quantities:
                raise BillingError("Order ID already exists with different request data.")
            return {"order_id": order_id, "already_exists": True, "status": "placed" if is_placed(conn, existing) else "requested"}
        date = conn.execute("SELECT CURRENT_DATE").fetchone()[0]
        subtotal = Decimal(0)
        for code, qty in quantities.items():
            item = conn.execute("SELECT * FROM items WHERE item_code=?", (code,)).fetchone()
            if item is None:
                raise BillingError(f"Item {code} not found.")
            if item["availablequantity"] < qty:
                raise BillingError(f"Insufficient inventory for {code}.")
            subtotal += price_for(conn, item, date) * qty
        values = totals(conn, subtotal)
        conn.execute("INSERT INTO orders(order_id,customer_id,subtotal,charged_price) VALUES (?,?,0,0)",
                     (order_id, customer_id))
        conn.executemany("INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)",
                         [(order_id, code, qty) for code, qty in quantities.items()])
        update_totals(conn, order_id, values)
        currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        conn.execute('INSERT INTO order_details(order_id,currency) VALUES (?,?)',
                     (order_id, currency[0] if currency else 'USD'))
    return {"order_id": order_id, "status": "requested", "estimate": values,
            "currency": currency[0] if currency else 'USD',
            "note": "Inventory is not reserved. Placement recalculates totals using order-date prices."}


@tool_result
def apply_promotion(order_id: str, customer_id: str, promo_code: str) -> dict:
    """Apply or replace a promotion on a draft only; same-code repeats are safe."""
    with transaction() as conn:
        order = order_row(conn, order_id, customer_id)
        if is_placed(conn, order):
            raise BillingError("Promotions can only change before order placement.")
        lines = lines_for(conn, order_id)
        if not lines:
            raise BillingError("Order has no requested items.")
        subtotal = sum((price_for(conn, line, order["order_date"]) * line["quantity"] for line in lines), Decimal(0))
        values = totals(conn, subtotal, promo_code)
        conn.execute("UPDATE orders SET promo_code=? WHERE order_id=?", (promo_code, order_id))
        update_totals(conn, order_id, values)
    return {"order_id": order_id, "promo_code": promo_code, **values}


@tool_result
def place_order(order_id: str, customer_id: str) -> dict:
    """Fulfill a confirmed draft atomically; repeat calls return the existing invoice."""
    with transaction() as conn:
        order = order_row(conn, order_id, customer_id)
        if is_placed(conn, order):
            return {"order_id": order_id, "invoice_number": order["order_invoice"], "already_placed": True}
        lines = lines_for(conn, order_id)
        if not lines:
            raise BillingError("Order has no requested items.")
        subtotal = Decimal(0)
        for line in lines:
            qty, code = line["quantity"], line["item_code"]
            price = price_for(conn, line, order["order_date"])
            amount = money(price * qty)
            changed = conn.execute("UPDATE items SET availablequantity=availablequantity-? "
                                   "WHERE item_code=? AND availablequantity>=?", (qty, code, qty))
            if changed.rowcount != 1:
                raise BillingError(f"Insufficient inventory for {code}.")
            conn.execute("INSERT INTO order_fulfilled(order_id,item_code,quantity) VALUES (?,?,?)", (order_id, code, qty))
            conn.execute("INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) "
                         "VALUES (?,?,?,?,?)", (order_id, code, qty, str(price), str(amount)))
            subtotal += amount
        values = totals(conn, subtotal, order["promo_code"])
        if any(money(order[key]) != money(value) for key, value in values.items()):
            raise BillingError('Draft prices or charges changed. Refresh the draft with manage_draft and confirm the new totals before placement.')
        configured_currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        draft_currency = conn.execute('SELECT currency FROM order_details WHERE order_id=?', (order_id,)).fetchone()
        if draft_currency and draft_currency[0] != (configured_currency[0] if configured_currency else 'USD'):
            raise BillingError('Billing currency changed after this draft. Create a new draft and confirm its currency and totals.')
        update_totals(conn, order_id, values)
        invoice = "INV-" + uuid4().hex[:24].upper()
        conn.execute("UPDATE orders SET order_invoice=? WHERE order_id=?", (invoice, order_id))
        currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        customer = dict(conn.execute("SELECT * FROM customers WHERE customer_id=?", (customer_id,)).fetchone())
        conn.execute("INSERT INTO order_details(order_id,currency,due_date,customer_snapshot) "
                     "VALUES (?,?,date('now','+30 days'),?) ON CONFLICT(order_id) DO UPDATE SET "
                     "currency=excluded.currency, customer_snapshot=excluded.customer_snapshot, "
                     "due_date=COALESCE(order_details.due_date,excluded.due_date)",
                     (order_id, currency[0] if currency else 'USD', json.dumps(customer)))
        due_date = conn.execute('SELECT due_date FROM order_details WHERE order_id=?', (order_id,)).fetchone()[0]
    return {"order_id": order_id, "invoice_number": invoice, "status": "placed", **values,
            "currency": currency[0] if currency else 'USD', 'due_date': due_date}


@tool_result
def process_refund(order_id: str, customer_id: str, reason: str, amount: str | None = None) -> dict:
    """Record one full or partial ledger refund. No external payment or inventory return occurs."""
    if not reason or not reason.strip() or len(reason) > 2000:
        raise BillingError("A refund reason of 1-2000 characters is required.")
    with transaction() as conn:
        order = order_row(conn, order_id, customer_id)
        if not is_placed(conn, order):
            raise BillingError("Only placed orders can be refunded.")
        if order["refund_status"] or order["refund_amount"] is not None:
            raise BillingError("A refund is already recorded; duplicate refunds are blocked.")
        remaining = money(order['charged_price'])
        refund = money(remaining if amount is None else amount)
        if refund <= 0 or refund > remaining:
            raise BillingError("Refund must be positive and no greater than the invoice total.")
        conn.execute("UPDATE orders SET refund_amount=?,refund_status='recorded',refund_reason=? WHERE order_id=?",
                     (str(refund), reason.strip(), order_id))
    return {"order_id": order_id, "refund_amount": str(refund), "refund_status": "recorded",
            "note": "Recorded in the local ledger only. No payment-provider refund was sent."}


@tool_result
def download_invoice(invoice_number: str, customer_id: str) -> dict:
    """Generate a PDF from persisted invoice lines for the specified customer."""
    from database.invoice import generate_invoice
    return generate_invoice(invoice_number, customer_id)













"""SQLite parser/authorizer is the security boundary, not a keyword regex."""
import math
import sqlite3
import time
from config import settings
from database.connection import connect

TABLES = {'billing_config', 'customers', 'items', 'promotions', 'orders',
          'order_requests', 'order_fulfilled', 'invoice_lines', 'price_history',
          'order_details', 'billing_audit',
          'product_details', 'product_attributes', 'product_relations', 'json_each'}
FUNCTIONS = set('abs avg coalesce count date datetime glob group_concat hex ifnull '
                'instr julianday length like lower ltrim max min nullif printf replace '
                'round rtrim strftime substr substring sum time total trim typeof '
                'unicode upper row_number rank dense_rank lag lead first_value last_value '
                'json_extract json_type json_valid json_array_length current_date current_time current_timestamp'.split())


def authorize(action, arg1, arg2, db, trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and arg1 in TABLES and db in ('main', None):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or '').lower() in FUNCTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 == 'table_info' and arg2 in TABLES:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def execute_sql_query(query: str | None = None, chart: dict | None = None, parameters: list | None = None) -> dict:
    """Answer flexible questions with one read-only SELECT/WITH or PRAGMA table_info(table).

    Product discovery uses qualified SQL against items and optional product_details.
    Count all matching products before displaying a page. Use stable ordering and LIMIT/OFFSET.
    Include out-of-stock products unless explicitly filtered. Preserve scope on next-page requests.
    Use effective price_history for current prices; overlapping prices are unknown, never guessed.

    Join/filter/group permitted base tables instead of needing a tool per question.
    Inspect columns with PRAGMA. No database views exist. Payment and credit records are unavailable.
    Invoice totals minus recorded refunds are not amounts due or proof of settlement.
    Use ? placeholders with parameters for user-provided values; never interpolate names into SQL.
    For requested admin visuals optionally pass chart={"type":"bar","title":"Revenue","x":"month","y":["revenue"]}.
    Types: bar,grouped_bar,horizontal_bar,stacked_bar,normalized_bar,line,step_line,area,stacked_area,
    normalized_area,pie,donut,scatter,bubble,histogram,boxplot,heatmap,waterfall,range,combo,timeline,
    candlestick,pareto,funnel,treemap. x/y and optional group,size,end must be returned column aliases.
    Histogram: numeric x,y:[],bins 2-50. Timeline: ISO x,end,group task label,y:[]. Heatmap: x,group,y:[measure].
    Bubble: numeric x,y:[measure],size. Range: y:[low,high]. Combo: y:[bar,line]. Candlestick: y:[open,high,low,close].
    Other types accept the column counts in the chart contract. Optional x_type: categorical,numeric,temporal.
    Distributions use complete observations; summaries aggregate duplicate coordinates in SQL.
    Include a currency column for money and query ONE currency per chart. Order trends chronologically.
    Chart queries allow up to 1000 rows; ordinary reads use SQL_MAX_ROWS. No charts from truncated results. A chart_error leaves query rows usable; correct SQL/spec if needed.
    """
    if not isinstance(query, str) or not query.strip() or len(query) > 20000:
        return {'ok': False, 'error': 'Provide one SQL query of at most 20000 characters.'}
    if parameters is not None and (not isinstance(parameters,list) or len(parameters)>100 or
            any(value is not None and not isinstance(value,(str,int,float)) for value in parameters)):
        return {'ok':False,'error':'Parameters must be a list of up to 100 text, numeric or null values.'}
    if any((isinstance(value, int) and not -(2**63) <= value < 2**63) or
           (isinstance(value, float) and not math.isfinite(value)) for value in parameters or []):
        return {'ok':False,'error':'Numeric parameters must be finite and integers must fit the SQLite 64-bit range.'}
    from mcp_server.access import scope, customer_query_connection
    access = scope.get()
    if chart is not None and (not access or access['mode'] != 'admin'):
        return {'ok': False, 'error': 'Analytics charts require Admin mode.'}
    conn = (customer_query_connection(access['customer_id'])
            if access and access['mode'] == 'customer' else connect(readonly=True))
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1000000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.set_authorizer(authorize)
        cursor = conn.execute(query, parameters or [])
        from mcp_server.charts import MAX_CHART_ROWS
        row_limit = MAX_CHART_ROWS if chart is not None else settings.SQL_MAX_ROWS
        rows = cursor.fetchmany(row_limit + 1)
        if any(isinstance(value, float) and not math.isfinite(value) for row in rows for value in row):
            return {'ok':False,'error':'The query produced a non-finite number. Narrow the calculation or correct its inputs.'}
        result = {'ok': True, 'columns': [c[0] for c in cursor.description or []],
                'rows': [list(row) for row in rows[:row_limit]],
                'row_count': min(len(rows), row_limit),
                'truncated': len(rows) > row_limit,
                'coverage_note': 'row_count counts returned SQL rows only. SQL LIMIT and filters may exclude matches even when truncated is false. Count the full matching set separately before claiming completeness, absence or a store-wide total.'}
        if chart is not None:
            from mcp_server.charts import prepare_chart
            try:
                payload = prepare_chart(result['columns'],result['rows'],chart,result['truncated'])
                result['chart'] = {key:value for key,value in payload.items() if key != 'data'}
            except ValueError as exc:
                result['chart_error'] = str(exc)
        return result
    except sqlite3.Error as exc:
        return {'ok': False, 'error': f'Query rejected or failed: {exc}'}
    finally:
        conn.close()



















"""Customer and draft workflows. No external money movement or message delivery."""
from contextlib import contextmanager
from decimal import Decimal
import re

from database.connection import connect, transaction
from mcp_server.tools.billing_tools import (BillingError, tool_result, identifier, money,
    order_row, is_placed, totals, update_totals, price_for)


def confirm(confirmed):
    if confirmed is not True:
        raise BillingError('Show the proposed change and obtain explicit confirmation first.')


def text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BillingError(f'{name} must contain 1-{maximum} characters.')
    return value.strip()


@contextmanager
def reading():
    conn = connect(readonly=True)
    try:
        conn.execute('BEGIN')
        yield conn
    finally:
        conn.close()


def ensure_customer(conn, customer_id):
    if not conn.execute('SELECT 1 FROM customers WHERE customer_id=?', (customer_id,)).fetchone():
        raise BillingError('Customer not found.')


@tool_result
def manage_customer(customer_id: str, name: str, company: str = '', email: str = '',
                    action: str = 'create', confirmed: bool = False) -> dict:
    """Create or update a customer after confirmation. Update replaces all contact fields; show them first.

    Existing issued invoice contact snapshots are preserved. Use SQL to look up a customer before editing.
    """
    confirm(confirmed)
    identifier(customer_id, 'customer ID')
    name = text(name, 'Name', 200)
    if not isinstance(company, str) or len(company) > 200:
        raise BillingError('Company must contain at most 200 characters.')
    if not isinstance(email, str) or len(email) > 200 or (email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email)):
        raise BillingError('Provide a valid email address or leave it empty.')
    with transaction() as conn:
        existing = conn.execute('SELECT * FROM customers WHERE customer_id=?', (customer_id,)).fetchone()
        if action == 'create':
            if existing:
                if (existing['name'], existing['company'] or '', existing['email'] or '') != (name, company, email):
                    raise BillingError('Customer ID exists with different details. Use update after confirmation.')
                return {'customer_id': customer_id, 'already_exists': True}
            conn.execute('INSERT INTO customers VALUES (?,?,?,?)', (customer_id, name, company, email))
        elif action == 'update':
            ensure_customer(conn, customer_id)
            conn.execute('UPDATE customers SET name=?,company=?,email=? WHERE customer_id=?', (name, company, email, customer_id))
        else:
            raise BillingError('Action must be create or update.')
    return {'customer_id': customer_id, 'name': name, 'company': company, 'email': email, 'action': action}


@tool_result
def manage_draft(order_id: str, customer_id: str, action: str, items: list[dict] | None = None,
                 reason: str = '', confirmed: bool = False) -> dict:
    """After confirmation, replace all lines of a draft or cancel it. action: replace_items or cancel.

    items is the complete desired list of item_code and integer quantity. Never edits issued invoices.
    Cancellation retains records for audit and does not restock since drafts never reserved stock.
    """
    confirm(confirmed)
    with transaction() as conn:
        existing_meta = conn.execute('SELECT status FROM order_details WHERE order_id=?', (order_id,)).fetchone()
        if existing_meta and existing_meta['status'] == 'cancelled' and action == 'cancel':
            owner = conn.execute('SELECT customer_id FROM orders WHERE order_id=?', (order_id,)).fetchone()
            if owner and owner[0] == customer_id:
                return {'order_id': order_id, 'status': 'cancelled', 'already_cancelled': True}
        order = order_row(conn, order_id, customer_id)
        if is_placed(conn, order):
            raise BillingError('Issued invoices cannot be edited. Review the supported refund workflow with store staff.')
        if action == 'cancel':
            reason = text(reason, 'Cancellation reason')
            currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
            conn.execute("INSERT INTO order_details(order_id,currency,status,cancellation_reason) VALUES (?,?,'cancelled',?) "
                         "ON CONFLICT(order_id) DO UPDATE SET status='cancelled',cancellation_reason=excluded.cancellation_reason",
                         (order_id, currency, reason))
            return {'order_id': order_id, 'status': 'cancelled', 'reason': reason}
        if action != 'replace_items' or not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise BillingError('Use cancel or replace_items with 1-100 line items.')
        quantities = {}
        for line in items:
            if not isinstance(line, dict):
                raise BillingError('Each item must contain item_code and quantity.')
            code = identifier(line.get('item_code'), 'item code')
            qty = line.get('quantity')
            if type(qty) is not int or not 1 <= qty <= 100000:
                raise BillingError('Quantity must be an integer from 1 to 100000.')
            quantities[code] = quantities.get(code, 0) + qty
            if quantities[code] > 100000:
                raise BillingError('Combined quantity exceeds 100000.')
        subtotal = Decimal(0)
        for code, qty in quantities.items():
            item = conn.execute('SELECT * FROM items WHERE item_code=?', (code,)).fetchone()
            if not item or item['availablequantity'] < qty:
                raise BillingError(f'Item {code} is unavailable or has insufficient stock.')
            subtotal += price_for(conn, item, order['order_date']) * qty
        values = totals(conn, subtotal, order['promo_code'])
        conn.execute('DELETE FROM order_requests WHERE order_id=?', (order_id,))
        conn.executemany('INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)',
                         [(order_id, code, qty) for code, qty in quantities.items()])
        update_totals(conn, order_id, values)
        return {'order_id': order_id, 'status': 'requested', 'estimate': values}





































"""Customer shopping comparisons; deterministic prices, no invented specifications."""
import json
import re
from decimal import Decimal
from mcp_server.tools.billing_tools import BillingError, tool_result, money, price_for
from mcp_server.tools.operations_tools import reading


def options(quantity, budget, required_features):
    if type(quantity) is not int or not 1 <= quantity <= 100000:
        raise BillingError('Quantity must be an integer from 1 to 100000.')
    budget = money(budget) if budget is not None else None
    required_features = required_features or {}
    if not isinstance(required_features, dict) or len(required_features) > 20 or any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, (str, int, float, bool))
        for k, v in required_features.items()
    ):
        raise BillingError('Required features must be a dictionary of at most 20 feature/value pairs.')
    return budget, required_features


def resolve(conn, reference):
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 200:
        raise BillingError('Provide an item code, SKU or product name.')
    reference = reference.strip()
    rows = conn.execute('SELECT * FROM items WHERE item_code=? OR sku=?', (reference, reference)).fetchall()
    if not rows:
        rows = conn.execute('SELECT * FROM items WHERE lower(name)=lower(?)', (reference,)).fetchall()
    if not rows:
        rows = conn.execute('SELECT * FROM items WHERE instr(lower(name),lower(?))>0 LIMIT 6', (reference,)).fetchall()
    if len(rows) != 1:
        choices = ', '.join(f"{r['item_code']} ({r['name']})" for r in rows[:5])
        raise BillingError('Product not found.' if not rows else 'Product is ambiguous. Ask the customer to choose: ' + choices)
    return rows[0]


def product(conn, row, today, quantity, budget, requirements):
    detail = conn.execute('SELECT * FROM product_details WHERE item_code=?', (row['item_code'],)).fetchone()
    features = json.loads(detail['features_json']) if detail else {}
    units = {}
    for attribute in conn.execute('SELECT * FROM product_attributes WHERE item_code=?', (row['item_code'],)):
        key = attribute['attribute']
        # Explicit typed attributes take precedence over a legacy JSON key (case-insensitively).
        for existing in list(features):
            if existing.casefold() == key.casefold():
                del features[existing]
        features[key] = attribute['text_value'] if attribute['text_value'] is not None else attribute['number_value']
        if attribute['unit']:
            units[key] = attribute['unit']
    unit = price_for(conn, row, today)
    total = money(unit * quantity)
    matched, missing, different = [], [], []
    for key, value in requirements.items():
        recorded_key = next((k for k in features if k.casefold()==key.casefold()),None)
        if recorded_key is None:
            missing.append(key)
        elif (str(features[recorded_key]).casefold() == str(value).casefold() or
              (type(features[recorded_key]) in (int,float) and type(value) in (int,float)
               and Decimal(str(features[recorded_key])) == Decimal(str(value)))):
            matched.append(key)
        else:
            different.append(key)
    return {'item_code': row['item_code'], 'sku': row['sku'], 'name': row['name'],
            'category': detail['category'] if detail else None,
            'description': detail['description'] if detail else '', 'features': features, 'feature_units': units,
            'unit_price': str(unit), 'quantity': quantity, 'merchandise_total': str(total),
            'available_quantity': row['availablequantity'], 'enough_stock': row['availablequantity'] >= quantity,
            'within_budget': budget is None or total <= budget,
            'matched_features': matched, 'unknown_features': missing, 'different_features': different}


def context(conn):
    config = dict(conn.execute('SELECT key,value FROM billing_config'))
    return conn.execute('SELECT CURRENT_DATE').fetchone()[0], config.get('currency', 'USD')


def eligible(row):
    return row['enough_stock'] and row['within_budget'] and not row['unknown_features'] and not row['different_features']


NOTES = ['Prices use today\'s dated price history, falling back to sellingprice, as in order quoting.',
         'Budget covers merchandise for the requested quantity, before promotions, tax and shipping. Request an order draft for the final total.',
         'Missing features are unknown. Price and stock do not establish quality, compatibility or suitability. No inventory is reserved.']


@tool_result
def compare_products(products: list[str], quantity: int = 1, budget: str | None = None,
                     required_features: dict | None = None) -> dict:
    """Compare 2-5 products by code/SKU/name, price, stock and recorded features.

    budget is the TOTAL merchandise budget for quantity. required_features maps exact stored feature
    names to desired values (e.g. {"connection":"wireless"}); missing features stay unknown.
    Suggests the lowest-priced available match, never an unsupported overall quality winner.
    Ask what 'better' means for the customer. Product names can be ambiguous; ask instead of guessing.
    """
    budget, requirements = options(quantity, budget, required_features)
    if not isinstance(products, list) or not 2 <= len(products) <= 5:
        raise BillingError('Compare between 2 and 5 products.')
    with reading() as conn:
        today, currency = context(conn)
        rows = [product(conn, resolve(conn, ref), today, quantity, budget, requirements) for ref in products]
        if len({r['item_code'] for r in rows}) != len(rows):
            raise BillingError('Choose distinct products to compare.')
        matches = [r for r in rows if eligible(r)]
        lowest = min((Decimal(r['unit_price']) for r in matches), default=None)
        winners = [r['item_code'] for r in matches if Decimal(r['unit_price']) == lowest]
        categories = {r['category'] for r in rows}
        return {'currency': currency, 'as_of': today, 'products': rows,
                'lowest_price_matching_items': winners,
                'recommendation_basis': 'Lowest current price among in-stock products meeting budget and every recorded requirement; ties retained.',
                'same_recorded_category': len(categories) == 1 and None not in categories,
                'notes': NOTES + ['Different or missing categories require checking that the products serve the same purpose.']}


def words(name):
    return set(re.findall(r'[a-z]+', name.lower())) - {
        'a', 'an', 'the', 'for', 'with', 'and', 'inch', 'cm', 'gb', 'tb', 'hd',
        'black', 'white', 'red', 'blue', 'premium', 'basic', 'pro', 'model', 'variant', 'wireless', 'usb'}


@tool_result
def find_cheaper_alternatives(product_reference: str, quantity: int = 1, budget: str | None = None,
                              required_features: dict | None = None, limit: int = 5) -> dict:
    """Find cheaper in-stock alternatives within the same recorded category.

    Without a source category, uses shared meaningful name words and labels matches tentative.
    Never treats a cheaper unrelated item as equivalent. Exact required features must be recorded
    and match. Budget is total merchandise cost for quantity, before tax/shipping/promotions.
    """
    budget, requirements = options(quantity, budget, required_features)
    if type(limit) is not int or not 1 <= limit <= 10:
        raise BillingError('Limit must be from 1 to 10.')
    with reading() as conn:
        today, currency = context(conn)
        source = product(conn, resolve(conn, product_reference), today, quantity, budget, requirements)
        if source['category']:
            candidates = conn.execute('SELECT i.* FROM items i JOIN product_details d ON d.item_code=i.item_code '
                'WHERE lower(d.category)=lower(?) AND i.item_code<>? AND i.availablequantity>=?',
                (source['category'], source['item_code'], quantity)).fetchall()
        else:
            candidates = conn.execute('SELECT * FROM items WHERE item_code<>? AND availablequantity>=?',
                                      (source['item_code'], quantity)).fetchall()
        choices, unavailable_prices = [], []
        for row in candidates:
            if not source['category'] and not words(source['name']).intersection(words(row['name'])):
                continue
            try:
                candidate = product(conn, row, today, quantity, budget, requirements)
            except BillingError:
                unavailable_prices.append(row['item_code'])
                continue
            if not eligible(candidate) or Decimal(candidate['unit_price']) >= Decimal(source['unit_price']):
                continue
            candidate['savings_for_quantity'] = str(money(Decimal(source['merchandise_total']) - Decimal(candidate['merchandise_total'])))
            candidate['match_basis'] = 'recorded_category' if source['category'] else 'shared_name_words_only'
            choices.append(candidate)
        choices.sort(key=lambda r: (Decimal(r['unit_price']), r['item_code']))
        return {'currency': currency, 'as_of': today, 'source': source, 'alternatives': choices[:limit],
                'matched_count': len(choices), 'truncated': len(choices) > limit,
                'items_with_unusable_prices': unavailable_prices,
                'notes': NOTES + ['Name-based matches are possible alternatives only; verify purpose and compatibility before recommending.',
                                 'No matches means no qualifying cheaper option was found; do not invent alternatives.']}
