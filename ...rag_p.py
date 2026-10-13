"""Trusted API/workbench scope carried over the private local stdio transport."""
from contextvars import ContextVar
from contextlib import closing
from functools import wraps
import inspect
import sqlite3
from database.connection import connect

scope = ContextVar('tool_scope', default=None)
ADMIN_TOOLS = {'execute_sql_query', 'billing_analysis', 'billing_anomalies', 'download_invoice'}
CUSTOMER_TOOLS = {'execute_sql_query', 'request_order', 'place_order', 'apply_promotion',
                  'download_invoice', 'manage_draft', 'manage_customer', 'process_refund',
                  'compare_products', 'find_cheaper_alternatives'}
SCOPE_FIELDS = ('execution_mode', 'execution_customer_id')


def scoped_tool(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        mode = kwargs.pop('execution_mode', 'customer')
        customer = kwargs.pop('execution_customer_id', None)
        allowed = ADMIN_TOOLS if mode == 'admin' else CUSTOMER_TOOLS if mode == 'customer' else set()
        if fn.__name__ not in allowed:
            return {'ok': False, 'error': 'This tool is not available in the current mode.'}
        arguments = inspect.signature(fn).bind(*args, **kwargs).arguments
        if mode == 'customer' and 'customer_id' in arguments:
            creating = fn.__name__ == 'manage_customer' and arguments.get('action', 'create') == 'create'
            if creating and customer is None:
                # An anonymous shopper may create a NEW account, never adopt an existing one.
                with closing(connect(readonly=True)) as conn:
                    exists = conn.execute('SELECT 1 FROM customers WHERE customer_id=?', (arguments['customer_id'],)).fetchone()
                if exists:
                    return {'ok': False, 'error': 'Existing customer: start a conversation with that customer selected.'}
            elif not customer or arguments['customer_id'] != customer:
                return {'ok': False, 'error': 'Select the matching customer in a new conversation first.'}
        token = scope.set({'mode': mode, 'customer_id': customer})
        try:
            return fn(*args, **kwargs)
        finally:
            scope.reset(token)
    run.__signature__ = inspect.signature(fn).replace(parameters=[
        *inspect.signature(fn).parameters.values(),
        inspect.Parameter('execution_mode', inspect.Parameter.KEYWORD_ONLY, default='customer', annotation=str),
        inspect.Parameter('execution_customer_id', inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None)])
    run.__annotations__ = {**fn.__annotations__, 'execution_mode': str, 'execution_customer_id': str | None}
    return run


def customer_query_connection(customer_id):
    """Materialize only public catalogue and associated customer rows in an isolated DB.

    User SQL never touches the source connection, preventing joins, CTEs and main.table
    qualification from bypassing a row filter. Original column names are preserved.
    """
    source, target = connect(readonly=True), sqlite3.connect(':memory:')
    target.row_factory = sqlite3.Row
    queries = {
        'billing_config': "SELECT key,value FROM billing_config WHERE key IN ('currency','tax_rate','shipping_amount')",
        'items': 'SELECT sku,item_code,name,sellingprice,availablequantity FROM items',
        'product_details': 'SELECT * FROM product_details',
        'product_attributes': 'SELECT * FROM product_attributes',
        'product_relations': 'SELECT * FROM product_relations',
        'price_history': 'SELECT * FROM price_history',
        'promotions': 'SELECT * FROM promotions',
        'customers': 'SELECT * FROM customers WHERE customer_id=?',
        'orders': 'SELECT * FROM orders WHERE customer_id=?',
    }
    for table in ('order_requests','order_fulfilled','invoice_lines','order_details'):
        queries[table] = f'SELECT t.* FROM {table} t JOIN orders o ON o.order_id=t.order_id WHERE o.customer_id=?'
    try:
        source.execute('BEGIN')
        for table, sql in queries.items():
            cursor = source.execute(sql, (customer_id,) if '?' in sql else ())
            columns = [c[0] for c in cursor.description]
            target.execute('CREATE TABLE '+table+' ('+', '.join('"'+c+'"' for c in columns)+')')
            target.executemany('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in columns)+')', cursor.fetchall())
        target.commit()
        return target
    except BaseException:
        target.close()
        raise
    finally:
        source.close()
