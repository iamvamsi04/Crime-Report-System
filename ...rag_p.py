INFO:     billing_model seconds=3.242 input_tokens=10307 output_tokens=89
billing_tool call_tool name=%s args=%s execute_sql_query {'query': "SELECT COALESCE(d.category,'Uncategorised') AS category, COUNT(*) AS products, SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code GROUP BY COALESCE(d.category,'Uncategorised') ORDER BY category;", 'parameters': None}
INFO:     billing_tool tool=execute_sql_query seconds=0.034
INFO:     billing_model seconds=3.050 input_tokens=10636 output_tokens=100
INFO:     billing_turn mode=customer total_seconds=6.558 lock_wait_seconds=0.000 tool_calls=1 tool_seconds=0.034
