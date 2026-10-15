NFO:     billing_api queue_wait_seconds=0.000
INFO:     billing_model seconds=4.972 input_tokens=6289 output_tokens=170
billing_tool call_tool name=%s args=%s execute_sql_query {'query': "SELECT strftime('%Y-%m', o.order_date) AS month, COALESCE(d.currency, (SELECT value FROM billing_config WHERE key='currency'), 'USD') AS currency, ROUND(SUM(COALESCE(d.subtotal,0) - COALESCE(d.discount_applied,0) + COALESCE(d.tax_amount,0) + COALESCE(d.shipping_amount,0)),2) AS revenue FROM orders o LEFT JOIN order_details d ON d.order_id=o.order_id WHERE o.order_invoice IS NOT NULL AND COALESCE(d.status,'active')<>'cancelled' GROUP BY month, currency ORDER BY month ASC", 'chart': {'type': 'bar', 'title': 'Monthly Revenue Trend (USD)', 'x': 'month', 'y': ['revenue']}}
INFO:     billing_tool tool=execute_sql_query seconds=0.012
INFO:     billing_model seconds=2.925 input_tokens=6488 output_tokens=73
INFO:     billing_turn mode=admin total_seconds=7.915 lock_wait_seconds=0.000 tool_calls=1 tool_seconds=0.012
