import sqlite3
import json
from contextlib import contextmanager
from contextvars import ContextVar
from config import settings

audit_context = ContextVar('billing_audit', default=None)


def connect(readonly=False):
    if readonly:
        conn = sqlite3.connect(settings.BILLING_DB.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    else:
        conn = sqlite3.connect(settings.BILLING_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        event = audit_context.get()
        if event and conn.total_changes:
            conn.execute('INSERT INTO billing_audit(action,details_json) VALUES (?,?)',
                         (event['action'], json.dumps(event['arguments'], default=str, sort_keys=True)))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    settings.BILLING_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        from database.migrations import remove_retired_tables, remove_views
        remove_views(conn)
        remove_retired_tables(conn)
        conn.executescript((settings.ROOT / 'database/schema.sql').read_text())
        conn.executescript((settings.ROOT / 'database/operations.sql').read_text())
        conn.executescript((settings.ROOT / 'database/insights.sql').read_text())
        conn.executemany('INSERT OR IGNORE INTO billing_config(key,value) VALUES (?,?)',
                         [('tax_rate', '0'), ('shipping_amount', '0'), ('currency', 'USD')])
        conn.commit()
    finally:
        conn.close()










-- Additive schema: existing product information, orders and financial records are untouched.
CREATE TABLE IF NOT EXISTS product_attributes (
    item_code TEXT NOT NULL REFERENCES items(item_code),
    attribute TEXT NOT NULL COLLATE NOCASE CHECK(length(trim(attribute)) BETWEEN 1 AND 100),
    text_value TEXT CHECK(text_value IS NULL OR length(text_value)<=500),
    number_value REAL CHECK(number_value IS NULL OR abs(number_value)<=1e15),
    unit TEXT CHECK(unit IS NULL OR length(unit)<=30),
    PRIMARY KEY(item_code,attribute),
    CHECK((text_value IS NOT NULL AND number_value IS NULL AND unit IS NULL)
       OR (text_value IS NULL AND number_value IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS ix_attributes_numeric ON product_attributes(attribute,unit,number_value);
CREATE INDEX IF NOT EXISTS ix_attributes_text ON product_attributes(attribute,text_value);
CREATE INDEX IF NOT EXISTS ix_orders_date ON orders(order_date);
CREATE TABLE IF NOT EXISTS product_relations (
    item_code TEXT NOT NULL REFERENCES items(item_code),
    related_item_code TEXT NOT NULL REFERENCES items(item_code),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('substitute','compatible','accessory')),
    notes TEXT NOT NULL DEFAULT '' CHECK(length(notes)<=2000),
    PRIMARY KEY(item_code,related_item_code,relation_type),
    CHECK(item_code<>related_item_code)
);
-- Relations are directional; store the reverse explicitly when appropriate.
CREATE INDEX IF NOT EXISTS ix_relations_reverse ON product_relations(related_item_code,relation_type);












import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.sax.saxutils import escape
from config import settings
from database.connection import connect


def invoice_path(invoice_number, customer_id):
    digest = hashlib.sha256(f"{customer_id}:{invoice_number}".encode()).hexdigest()
    return settings.INVOICE_DIR / f"{digest}.pdf"


def generate_invoice(invoice_number, customer_id):
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from mcp_server.tools.billing_tools import BillingError, money
    conn = connect(readonly=True)
    try:
        conn.execute("BEGIN")
        order = conn.execute("SELECT o.*,c.name,c.company,c.email FROM orders o JOIN customers c "
                             "ON c.customer_id=o.customer_id WHERE o.order_invoice=? AND o.customer_id=?",
                             (invoice_number, customer_id)).fetchone()
        if order is None:
            raise BillingError("Invoice not found for this customer.")
        lines = conn.execute("SELECT l.*,i.name FROM invoice_lines l JOIN items i ON i.item_code=l.item_code "
                             "WHERE l.order_id=? ORDER BY l.id", (order["order_id"],)).fetchall()
        currency = conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()
        currency = currency[0] if currency else "USD"
        metadata = conn.execute('SELECT currency,due_date,customer_snapshot FROM order_details WHERE order_id=?', (order['order_id'],)).fetchone()
        currency = metadata['currency'] if metadata else currency
        contact = json.loads(metadata['customer_snapshot']) if metadata and metadata['customer_snapshot'] else dict(order)
    finally:
        conn.close()
    if not lines:
        raise BillingError("Invoice has no persisted lines.")
    styles = getSampleStyleSheet()
    def p(text, style="BodyText"):
        return Paragraph(escape(str(text)), styles[style])
    story = [p("INVOICE", "Title"), p(invoice_number, "Heading2"),
             p(f"Order {order['order_id']} | Order date {order['order_date']}"), Spacer(1, 16),
             p(contact["name"], "Heading2")]
    for field in ("company", "email", "customer_id"):
        if contact[field]:
            story.append(p(contact[field]))
    story.append(p('Due date: ' + ((metadata['due_date'] if metadata else None) or 'Not recorded')))
    story.append(Spacer(1, 18))
    data = [[p(x) for x in ("Item", "Qty", "Unit price", "Amount")]]
    for line in lines:
        data.append([p(f"{line['name']} ({line['item_code']})"), p(line["qty_billed"]),
                     p(money(line["unit_price_billed"])), p(money(line["line_total"]))])
    table = Table(data, colWidths=[255, 40, 90, 90], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e7edf5")),
                               ("GRID", (0, 0), (-1, -1), .5, colors.HexColor("#d9d9d9")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 9),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    story.extend([table, Spacer(1, 20)])
    for label, field in [("Subtotal", "subtotal"), ("Discount", "discount_applied"),
                         ("Tax", "tax_amount"), ("Shipping", "shipping_amount"), ("Total", "charged_price")]:
        story.append(p(f"{label}: {currency} {money(order[field])}", "Heading3" if field == "charged_price" else "BodyText"))
    if order["refund_amount"] is not None:
        story.extend([Spacer(1, 12), p(f"Ledger refund: {currency} {money(order['refund_amount'])} ({order['refund_status']})"),
                      p(order["refund_reason"] or ""), p("This ledger entry does not confirm a payment-provider refund.")])
    story.append(Spacer(1, 12))
    net = money(order['charged_price']) - money(order['refund_amount'] or 0)
    story.append(p(f'Invoice total after recorded refunds: {currency} {net}'))
    story.append(p('Payment status and outstanding balance are not tracked. This invoice is not proof of payment.'))
    path = invoice_path(invoice_number, customer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=".pdf", delete=False) as handle:
            temp = Path(handle.name)
        SimpleDocTemplate(str(temp), pagesize=A4, rightMargin=60, leftMargin=60,
                          title=f"Invoice {invoice_number}").build(story)
        os.replace(temp, path)
    finally:
        if temp and temp.exists():
            temp.unlink()
    return {"invoice_number": invoice_number, "customer_id": customer_id,
            "filename": "invoice.pdf", "ready": True}














"""Remove retired financial and support storage, preserving populated legacy databases first."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import logging
import sqlite3
from uuid import uuid4

from config import settings

RETIRED_TABLES = ('billing_case_events', 'billing_cases', 'billing_disputes', 'payments', 'credit_notes')


def remove_retired_tables(conn, backup_dir=None):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    retired = [name for name in RETIRED_TABLES if name in existing]
    if not retired:
        return None
    conn.execute('BEGIN IMMEDIATE')
    backup = None
    try:
        if any(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in retired):
            path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            if not path.is_file():
                raise ValueError('Cannot remove populated legacy tables without a file backup.')
            folder = Path(backup_dir) if backup_dir else settings.ROOT.parent / 'previous_versions/database_backups'
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = folder / f'{path.stem}-before-table-cleanup-{stamp}-{uuid4().hex[:8]}.db'
            # A separate read connection can snapshot the committed database while
            # this connection holds the reserved write lock, before any drops.
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Legacy backup failed its integrity check.')
            logging.getLogger(__name__).warning('Retired records preserved in %s', backup)
        for table in retired:
            conn.execute(f'DROP TABLE {table}')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Table cleanup would leave broken references.')
        conn.commit()
        return str(backup) if backup else None
    except BaseException:
        conn.rollback()
        raise


def remove_views(conn):
    """Drop stored query definitions atomically; underlying records are preserved."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            conn.execute('DROP VIEW ' + quoted)
        conn.commit()
        return names
    except BaseException:
        conn.rollback()
        raise












-- Additive migration. Original orders and invoice lines remain intact.
CREATE TABLE IF NOT EXISTS order_details (
    order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
    due_date TEXT,
    currency TEXT NOT NULL,
    customer_snapshot TEXT,
    cancellation_reason TEXT
);
CREATE TABLE IF NOT EXISTS billing_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON billing_audit
BEGIN SELECT RAISE(ABORT, 'Audit records cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON billing_audit
BEGIN SELECT RAISE(ABORT, 'Audit records cannot be deleted'); END;
CREATE INDEX IF NOT EXISTS ix_order_due ON order_details(due_date,status);









PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS billing_config (
    key VARCHAR(50) PRIMARY KEY, value VARCHAR(100) NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    customer_id VARCHAR(50) PRIMARY KEY, name VARCHAR(200) NOT NULL,
    company VARCHAR(200), email VARCHAR(200)
);
CREATE TABLE IF NOT EXISTS items (
    sku VARCHAR(50) PRIMARY KEY, item_code VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    baseprice DECIMAL(10,2) NOT NULL CHECK(baseprice >= 0),
    sellingprice DECIMAL(10,2) NOT NULL CHECK(sellingprice >= 0),
    availablequantity INTEGER NOT NULL DEFAULT 0 CHECK(availablequantity >= 0)
);
CREATE TABLE IF NOT EXISTS promotions (
    promo_code VARCHAR(50) PRIMARY KEY, description VARCHAR(300),
    discount_type VARCHAR(20) NOT NULL CHECK(discount_type IN ('percent','flat')),
    discount_value DECIMAL(10,2) NOT NULL CHECK(discount_value >= 0),
    CHECK(discount_type != 'percent' OR discount_value <= 100)
);
CREATE TABLE IF NOT EXISTS orders (
    order_id VARCHAR(50) PRIMARY KEY,
    order_date DATE NOT NULL DEFAULT CURRENT_DATE,
    customer_id VARCHAR(50) NOT NULL REFERENCES customers(customer_id),
    order_invoice VARCHAR(100),
    subtotal DECIMAL(10,2) NOT NULL CHECK(subtotal >= 0),
    tax_amount DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(tax_amount >= 0),
    shipping_amount DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(shipping_amount >= 0),
    discount_applied DECIMAL(10,2) NOT NULL DEFAULT 0 CHECK(discount_applied >= 0),
    charged_price DECIMAL(10,2) NOT NULL CHECK(charged_price >= 0),
    promo_code VARCHAR(50) REFERENCES promotions(promo_code),
    refund_amount DECIMAL(10,2), refund_status VARCHAR(20), refund_reason TEXT,
    CHECK(refund_amount IS NULL OR (refund_amount > 0 AND refund_amount <= charged_price))
);
CREATE TABLE IF NOT EXISTS order_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    quantity INTEGER NOT NULL CHECK(quantity > 0), UNIQUE(order_id,item_code)
);
CREATE TABLE IF NOT EXISTS order_fulfilled (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    quantity INTEGER NOT NULL CHECK(quantity > 0), UNIQUE(order_id,item_code)
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id VARCHAR(50) NOT NULL REFERENCES orders(order_id),
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    qty_billed INTEGER NOT NULL CHECK(qty_billed > 0),
    unit_price_billed DECIMAL(10,2) NOT NULL CHECK(unit_price_billed >= 0),
    line_total DECIMAL(10,2) NOT NULL CHECK(line_total >= 0), UNIQUE(order_id,item_code)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code VARCHAR(50) NOT NULL REFERENCES items(item_code),
    unit_price DECIMAL(10,2) NOT NULL CHECK(unit_price >= 0),
    effective_from DATE NOT NULL, effective_to DATE,
    CHECK(effective_to IS NULL OR effective_to >= effective_from)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_invoice_number ON orders(order_invoice)
    WHERE order_invoice IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_customer_orders ON orders(customer_id,order_date);
CREATE INDEX IF NOT EXISTS ix_price_dates ON price_history(item_code,effective_from,effective_to);

CREATE TABLE IF NOT EXISTS product_details (
    item_code TEXT PRIMARY KEY REFERENCES items(item_code),
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    features_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(features_json) AND json_type(features_json)='object')
);
CREATE INDEX IF NOT EXISTS ix_product_category ON product_details(category);









"""Explicit, repeatable demo seed. Never runs automatically on application startup."""
from database.connection import initialize, transaction


def seed():
    initialize()
    with transaction() as conn:
        conn.executemany("INSERT OR IGNORE INTO customers VALUES (?,?,?,?)", [
            ("CUST001", "Alex Morgan", "Northwind Studio", "alex@example.com"),
            ("CUST002", "Jamie Chen", "Contoso Labs", "jamie@example.com"),
            ("CUST003", "Sam Rivera", None, "sam@example.com"),
            ("CUST004", "Taylor Reed", "Fabrikam", "taylor@example.com")])
        conn.executemany("INSERT OR IGNORE INTO items VALUES (?,?,?,?,?,?)", [
            ("SKU-KB", "ITEM001", "Mechanical keyboard", "45", "79.90", 40),
            ("SKU-MS", "ITEM002", "Wireless mouse", "15", "29.50", 60),
            ("SKU-MN", "ITEM003", "27 inch monitor", "150", "249.00", 15)])
        conn.executemany("INSERT OR IGNORE INTO promotions VALUES (?,?,?,?)", [
            ("SAVE10", "Ten percent off merchandise", "percent", "10"),
            ("SAVE500", "500 off merchandise, capped at subtotal", "flat", "500")])
    print("Demo customers, items and promotions are ready. Existing records were preserved.")


if __name__ == "__main__":
    seed()
