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
