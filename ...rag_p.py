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
