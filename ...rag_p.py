"""Create a complete fictional billing dataset, atomically and without Azure.

Run: python generate_seed.py
Options: --db database/demo.db --seed 42 --as-of 2026-10-08
Relative database paths resolve against the project folder. The default is BILLING_DB.
Existing records/config are preserved. Refuse batches exceeding 500 main-table rows.
The fake_seed_v1 configuration marker prevents duplicate inserts on subsequent runs.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import random
import sqlite3

from config import settings
from mcp_server.tools.billing_tools import money, price_for, totals, update_totals

MAIN_COUNTS = {"customers": 400, "items": 400, "orders": 350,
               "order_requests": 450, "order_fulfilled": 400, "invoice_lines": 400}
TABLES = (*MAIN_COUNTS, "price_history", "promotions", "billing_config")
MARKER = "fake_seed_v1"
FIRST = "Alex Jamie Sam Taylor Morgan Jordan Casey Avery Riley Cameron Quinn Devon".split()
LAST = "Morgan Chen Rivera Reed Patel Smith Lewis Brooks Park Carter Diaz Evans".split()
PRODUCTS = [
    ("Mechanical keyboard", 7990), ("Wireless mouse", 2950), ("27 inch monitor", 24900),
    ("USB-C hub", 4495), ("Laptop stand", 3990), ("HD webcam", 6490),
    ("Noise cancelling headset", 12900), ("External SSD", 10990),
    ("Desk lamp", 3490), ("Office chair", 18900), ("Ethernet adapter", 1990),
    ("Bluetooth speaker", 5990), ("Power bank", 4590), ("Monitor arm", 6990),
    ("USB cable", 1290), ("Laptop sleeve", 2490), ("Desk mat", 1790),
    ("Wi-Fi router", 8990), ("Portable projector", 34900), ("Document scanner", 22900),
]


def counts(conn):
    return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(conn, opening_stock):
    """Check references, prices, totals, refunds and inventory before committing."""
    require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "Foreign key violations found.")
    require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity check failed.")
    for table, count in counts(conn).items():
        if table in MAIN_COUNTS:
            require(300 <= count <= 500, f"{table} has {count} rows outside 300-500.")
    for order in conn.execute("SELECT * FROM orders WHERE order_id GLOB 'FAKE-ORD-*'").fetchall():
        oid = order["order_id"]
        requested = dict(conn.execute("SELECT item_code,quantity FROM order_requests WHERE order_id=?", (oid,)))
        fulfilled = dict(conn.execute("SELECT item_code,quantity FROM order_fulfilled WHERE order_id=?", (oid,)))
        invoice = conn.execute("SELECT * FROM invoice_lines WHERE order_id=?", (oid,)).fetchall()
        require(bool(requested), f"{oid} has no requested items.")
        subtotal = Decimal(0)
        if order["order_invoice"]:
            require(requested == fulfilled, f"{oid} request and fulfillment mismatch.")
            require({r["item_code"]: r["qty_billed"] for r in invoice} == requested,
                    f"{oid} billed quantities do not match fulfillment.")
            for line in invoice:
                item = conn.execute("SELECT * FROM items WHERE item_code=?", (line["item_code"],)).fetchone()
                price = price_for(conn, item, order["order_date"])
                require(money(line["unit_price_billed"]) == price, f"{oid} incorrect historical price.")
                require(money(line["line_total"]) == money(price * line["qty_billed"]), f"{oid} incorrect line total.")
                subtotal += money(line["line_total"])
        else:
            require(not invoice and not fulfilled, f"{oid} draft has billed or fulfilled lines.")
            for code, quantity in requested.items():
                item = conn.execute("SELECT * FROM items WHERE item_code=?", (code,)).fetchone()
                require(item["availablequantity"] >= quantity, f"{oid} draft quantity exceeds stock.")
                subtotal += price_for(conn, item, order["order_date"]) * quantity
        for field, amount in totals(conn, subtotal, order["promo_code"]).items():
            require(money(order[field]) == money(amount), f"{oid}: {field} is inconsistent.")
        if order["refund_amount"] is not None:
            require(bool(order["order_invoice"]) and order["refund_status"] == "recorded"
                    and bool(order["refund_reason"]), f"{oid} invalid refund metadata.")
            require(0 < money(order["refund_amount"]) <= money(order["charged_price"]), f"{oid} invalid refund amount.")
        else:
            require(order["refund_status"] is None and order["refund_reason"] is None, f"{oid} incomplete refund fields.")
    for code, initial in opening_stock.items():
        sold = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM order_fulfilled WHERE item_code=?", (code,)).fetchone()[0]
        remaining = conn.execute("SELECT availablequantity FROM items WHERE item_code=?", (code,)).fetchone()[0]
        require(remaining == initial - sold and remaining >= 0, f"{code} inventory mismatch.")
        history = conn.execute("SELECT * FROM price_history WHERE item_code=? ORDER BY effective_from", (code,)).fetchall()
        for old, new in zip(history, history[1:]):
            require(old["effective_to"] is not None and old["effective_to"] < new["effective_from"],
                    f"{code} overlapping price history.")


def generate(db_path: Path, seed: int, as_of: date):
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # Schema creation and all seed records share a single rollback boundary.
        conn.executescript("BEGIN IMMEDIATE;\n" + (settings.ROOT / "database/schema.sql").read_text(encoding="utf-8"))
        if conn.execute("SELECT 1 FROM billing_config WHERE key=?", (MARKER,)).fetchone():
            result = {"status": "already_seeded", "database": str(db_path), "rows": counts(conn)}
            conn.rollback()
            return result
        before = counts(conn)
        for table, addition in MAIN_COUNTS.items():
            require(before[table] + addition <= 500,
                    f"Existing {table} rows ({before[table]}) plus this batch ({addition}) exceed 500. "
                    "No data changed. Use --db database/demo.db for a separate dataset.")
        for table, column, pattern in [("customers", "customer_id", "FAKE-CUST-*"),
                ("items", "item_code", "FAKE-ITEM-*"), ("orders", "order_id", "FAKE-ORD-*"),
                ("promotions", "promo_code", "FAKE-*")]:
            require(not conn.execute(f"SELECT 1 FROM {table} WHERE {column} GLOB ?", (pattern,)).fetchone(),
                    f"Existing FAKE identifiers found in {table}; use a fresh database.")
        conn.executemany("INSERT OR IGNORE INTO billing_config(key,value) VALUES (?,?)",
                         [("tax_rate", "7.50"), ("shipping_amount", "12.50"), ("currency", "USD")])
        customers = [f"FAKE-CUST-{i:04d}" for i in range(1, 401)]
        for i, cid in enumerate(customers, 1):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            company = f"Demo {rng.choice(['Cedar', 'Harbor', 'Maple', 'Summit', 'Willow'])} {rng.choice(['Studio', 'Labs', 'Services', 'Trading'])} {i:03d}"
            conn.execute("INSERT INTO customers VALUES (?,?,?,?)",
                         (cid, name, company if i % 5 else None, f"customer{i:04d}@example.com"))
        opening_stock = {}
        item_codes = []
        for i in range(1, 401):
            code = f"FAKE-ITEM-{i:04d}"
            name, base_cents = PRODUCTS[(i - 1) % len(PRODUCTS)]
            selling = money(Decimal(base_cents + rng.randint(-200, 800)) / 100)
            cost = money(selling * Decimal("0.60"))
            stock = rng.randint(40, 180)
            opening_stock[code] = stock
            item_codes.append(code)
            conn.execute("INSERT INTO items VALUES (?,?,?,?,?,?)",
                         (f"FAKE-SKU-{i:04d}", code, f"{name} - Demo model {i:03d}", str(cost), str(selling), stock))
            if i <= 80:
                for factor, start, end in [
                    (Decimal("0.85"), as_of-timedelta(days=365), as_of-timedelta(days=121)),
                    (Decimal("0.92"), as_of-timedelta(days=120), as_of-timedelta(days=31)),
                    (Decimal("1.00"), as_of-timedelta(days=30), None),
                ]:
                    conn.execute("INSERT INTO price_history(item_code,unit_price,effective_from,effective_to) VALUES (?,?,?,?)",
                                 (code, str(money(selling * factor)), start.isoformat(), end.isoformat() if end else None))
        promos = []
        for kind, values in [("percent", [5, 10, 15, 20, 25, 30]), ("flat", [5, 10, 20, 40, 75, 100])]:
            for value in values:
                code = f"FAKE-{kind.upper()}-{value}"
                promos.append(code)
                conn.execute("INSERT INTO promotions VALUES (?,?,?,?)",
                             (code, f"Fictional demo {kind} discount {value}", kind, str(value)))
        rng.shuffle(item_codes)
        cursor = 0
        refunded = {i: ("full" if n % 2 == 0 else "partial")
                    for n, i in enumerate(rng.sample(range(1, 301), 60))}
        refund_counts = {"full": 0, "partial": 0}
        for i in range(1, 351):
            oid = f"FAKE-ORD-{i:04d}"
            placed = i <= 300
            order_date = as_of - timedelta(days=rng.randint(0, 179)) if placed else as_of
            promo = promos[(i-1) % len(promos)] if i % 4 != 0 else None
            conn.execute("INSERT INTO orders(order_id,order_date,customer_id,subtotal,charged_price,promo_code) VALUES (?,?,?,0,0,?)",
                         (oid, order_date.isoformat(), rng.choice(customers), promo))
            subtotal = Decimal(0)
            for _ in range(2 if i <= 100 else 1):
                code = item_codes[cursor % len(item_codes)]
                cursor += 1
                quantity = rng.randint(1, 6)
                item = conn.execute("SELECT * FROM items WHERE item_code=?", (code,)).fetchone()
                price = price_for(conn, item, order_date.isoformat())
                amount = money(price * quantity)
                subtotal += amount
                conn.execute("INSERT INTO order_requests(order_id,item_code,quantity) VALUES (?,?,?)", (oid, code, quantity))
                if placed:
                    conn.execute("INSERT INTO order_fulfilled(order_id,item_code,quantity) VALUES (?,?,?)", (oid, code, quantity))
                    conn.execute("INSERT INTO invoice_lines(order_id,item_code,qty_billed,unit_price_billed,line_total) VALUES (?,?,?,?,?)",
                                 (oid, code, quantity, str(price), str(amount)))
                    updated = conn.execute("UPDATE items SET availablequantity=availablequantity-? WHERE item_code=? AND availablequantity>=?",
                                           (quantity, code, quantity))
                    require(updated.rowcount == 1, f"Insufficient stock for {code}.")
            values = totals(conn, subtotal, promo)
            update_totals(conn, oid, values)
            if placed:
                conn.execute("UPDATE orders SET order_invoice=? WHERE order_id=?", (f"INV-FAKE-{as_of.year}-{i:04d}", oid))
            total = money(values["charged_price"])
            if i in refunded and total > 0:
                kind = refunded[i]
                refund = total if kind == "full" else money(total * Decimal("0.25"))
                if refund > 0:
                    reason = "Fictional full return - ledger entry only" if kind == "full" else "Fictional partial credit for damaged packaging - ledger entry only"
                    conn.execute("UPDATE orders SET refund_amount=?,refund_status='recorded',refund_reason=? WHERE order_id=?",
                                 (str(refund), reason, oid))
                    refund_counts[kind] += 1
        audit(conn, opening_stock)
        conn.execute("INSERT INTO billing_config(key,value) VALUES (?,?)", (MARKER, f"seed={seed};as_of={as_of.isoformat()}"))
        after = counts(conn)
        result = {"status": "inserted", "database": str(db_path), "rows": after,
                  "added": {table: after[table]-before[table] for table in TABLES},
                  "placed_orders_added": 300, "draft_orders_added": 50,
                  "ledger_refunds_added": refund_counts, "validated": True}
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=settings.BILLING_DB, help="SQLite file; relative paths use the project folder.")
    parser.add_argument("--seed", type=int, default=42, help="Deterministic random seed.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(), help="Order date anchor, YYYY-MM-DD.")
    args = parser.parse_args()
    path = args.db if args.db.is_absolute() else settings.ROOT / args.db
    try:
        print(json.dumps(generate(path, args.seed, args.as_of), indent=2))
    except (ValueError, sqlite3.Error, ArithmeticError) as exc:
        parser.exit(1, f"Seed failed; transaction rolled back: {exc}\n")


if __name__ == "__main__":
    main()

