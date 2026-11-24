"""SQLite parser/authorizer is the security boundary, not a keyword regex."""
import math
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from threading import RLock
from config import settings
from database.connection import CASE_INSENSITIVE_COLUMNS, connect
from database.product_search import normalize_product_filters, register_product_search

TABLES = {'billing_config', 'customers', 'items', 'promotions', 'orders',
          'order_requests', 'order_fulfilled', 'invoice_lines', 'price_history',
          'order_details', 'billing_audit', 'refund_requests', 'refund_details', 'order_service',
          'customer_billing_details','invoice_revisions','store_credit_ledger',
          'customer_addresses','order_delivery',
          'product_details', 'product_attributes', 'product_relations', 'json_each'}
FUNCTIONS = set('abs avg coalesce count date datetime glob group_concat hex ifnull '
                'instr julianday length like lower ltrim max min nullif printf replace '
                'round rtrim strftime substr substring sum time total trim typeof '
                'unicode upper row_number rank dense_rank lag lead first_value last_value '
                'json_extract json_type json_valid json_array_length current_date current_time current_timestamp '
                'product_text product_matches product_like'.split())

# Use the same whitespace characters as the invoice lifecycle migration.
CANCELLED_FROM_SQL = (
    "CASE WHEN o.order_status = 'cancelled' THEN CASE WHEN "
    "TRIM(COALESCE(o.order_invoice, ''), CAST(X'090A0D20' AS TEXT)) = '' "
    "THEN 'draft' ELSE 'placed' END END"
)

# Store schema text only. Each lookup still checks the current database file and
# SQLite schema version; prices, customers and other business rows are never cached.
_SCHEMA_CACHE_LIMIT = 12
_schema_cache = OrderedDict()
_schema_cache_lock = RLock()


def _database_identity(path):
    stat = path.stat()
    return (str(path), stat.st_dev, stat.st_ino, stat.st_ctime_ns)


# Repair bare SQL aliases only; SQLite's read-only authorizer remains the security boundary.
def quote_reserved_aliases(query):
    """Quote bare explicit aliases only; never change literals, comments or values.

    This is syntax repair, not an authorization boundary. SQLite still parses the
    result under the same read-only authorizer and execution deadline.
    """
    tokens = re.findall(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|\w+|[^\w]", query)
    reserved = {'GROUP','ORDER','LIMIT','OFFSET','SELECT','FROM','WHERE','HAVING',
                'VALUES','INDEX','TABLE','JOIN','DISTINCT','UNION','CASE','WHEN','END'}
    previous = None
    repaired = []
    for token in tokens:
        if token.isspace() or token.startswith(('--','/*')):
            repaired.append(token)
            continue
        upper = token.upper()
        repaired.append('"'+token+'"' if previous == 'AS' and upper in reserved else token)
        previous = upper
    return ''.join(repaired)


def schema_hint(mode='customer'):
    """Return current, role-scoped schema metadata without reading business rows."""
    path = settings.BILLING_DB.resolve()
    identity = _database_identity(path)
    conn = connect(readonly=True)
    try:
        # Read the version and columns from one snapshot if another process runs
        # a migration. Replacements at the same path get a different file key.
        conn.execute('BEGIN')
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        key = (identity, version, mode, tuple(sorted(TABLES)))
        unchanged = identity == _database_identity(path)
        if unchanged:
            with _schema_cache_lock:
                cached = _schema_cache.get(key)
                if cached is not None:
                    _schema_cache.move_to_end(key)
                    return cached
        tables = sorted(TABLES - {'json_each'} - ({'billing_audit'} if mode != 'admin' else set()))
        if mode == 'customer':
            tables = [table for table in tables if table != 'promotions']
        elif mode == 'cashier':
            tables = [table for table in tables if table != 'customer_addresses']
        columns = {}
        for table in tables:
            names = [r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if mode != 'admin' and table == 'items':
                names = [n for n in names if n in {'sku','item_code','name','sellingprice','availablequantity'}]
            if names:
                columns[table] = names
        lines = ['CURRENT SQL TABLE MAP (metadata only). Qualify columns with table aliases. '
                 'Join the table that owns each requested column; do not guess column locations.']
        lines.extend(f'{table}({", ".join(names)})' for table,names in columns.items())
        joins = []
        for table in columns:
            for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                if fk['table'] in columns and fk['from'] in columns[table] and fk['to'] in columns[fk['table']]:
                    joins.append(f'{table}.{fk["from"]} = {fk["table"]}.{fk["to"]}')
        lines.append('Declared join keys: ' + '; '.join(joins))
        insensitive = [f'{table}.{name}' for table, names in columns.items()
                       for name in names if name.casefold() in CASE_INSENSITIVE_COLUMNS.get(table, ())]
        lines.append('Case-insensitive descriptive columns (COLLATE NOCASE): ' + ', '.join(insensitive)
                     + '. Other columns retain their declared comparison rules.')
        lines.append("Canonical status values: orders.order_status=draft/placed/cancelled; "
                     "order_details.status=active/cancelled; refund_requests.status=pending/approved; "
                     "order_service.fulfillment_status=unknown/placed/shipped/delivered/cancelled. "
                     "Use canonical lowercase values for writes.")
        if {'order_status', 'order_invoice'} <= set(columns.get('orders', ())):
            lines.append("Cancellation history: cancelled covers both stages. A cancelled draft never had an "
                         "issued invoice; a cancellation after placement retains its issued invoice. "
                         "For cancellation lists or split counts, use orders AS o and project "
                         f"{CANCELLED_FROM_SQL} AS cancelled_from. This is a derived alias. "
                         "Use cancelled_from in tool results when present. Describe the stage explicitly. "
                         "A cancelled draft is ineligible for an order refund because it was never placed or invoiced. "
                         "Explain this lifecycle reason before mentioning missing refund requests. "
                         "For a placed cancellation, check the current "
                         "refund record before stating any refund outcome.")
        lines.append('Aggregate each one-to-many side before combining totals. Missing columns are unavailable; do not invent them.')
        if 'price_history' in columns:
            lines.append("Prices use UTC timestamp intervals [effective_from,effective_to), with NULL end open. "
                         "For current SQL use julianday(effective_from)<=julianday('now') AND "
                         "(effective_to IS NULL OR julianday('now')<julianday(effective_to)+"
                         "CASE WHEN length(trim(effective_to))=10 THEN 1 ELSE 0 END). "
                         "Legacy date-only ends include that day. SQLite date functions round sub-millisecond values; "
                         "product tools use exact timestamp prices. Draft pricing uses order_details.price_as_of; "
                         "issued prices use invoice_lines.")
        lines.append('Product search: product_matches(i.name, ?) or product_matches(d.category, ?) matches words/phrases regardless of case, spaces, underscores, hyphens and common plurals. Use only on product text; IDs, prices and other fields keep exact semantics.')
        hint = '\n'.join(lines)
        if unchanged and identity == _database_identity(path):
            with _schema_cache_lock:
                _schema_cache[key] = hint
                _schema_cache.move_to_end(key)
                while len(_schema_cache) > _SCHEMA_CACHE_LIMIT:
                    _schema_cache.popitem(last=False)
        return hint
    finally:
        conn.close()


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


def _sql_error_details(exc, mode):
    message = str(exc)
    result = {'ok': False, 'error': f'Query rejected or failed: {message}'}
    lower = message.lower()
    if any(term in lower for term in ('no such column', 'ambiguous column', 'no such table')):
        schema = schema_hint(mode)
        result['schema_hint'] = schema
        missing = re.fullmatch(r'(?:no such|ambiguous) column(?: name)?: (?:\w+\.)*(\w+)', message, re.I)
        if missing:
            column = missing[1].lower()
            owners = [f'{table}.{name}'
                      for table, fields in re.findall(r'^(\w+)\(([^\n]*)\)$', schema, re.M)
                      for name in fields.split(', ') if name.lower() == column]
            if owners:
                result['repair_hint'] = ('Qualify the column and join its live source before referencing it. '
                                         'Matching columns: ' + ', '.join(owners) + '.')
    elif 'syntax error' in lower:
        result['repair_hint'] = ('Use SQLite syntax with all FROM/JOIN clauses before WHERE, GROUP BY or ORDER BY. '
                                 'Use direct joins and group observed rows when a generated grid is unnecessary.')
    return result


def execute_sql_query(query: str | None = None, chart: dict | None = None, parameters: list | None = None,
                      queries: list[dict] | None = None, report: str | None = None,
                      expected_chart_types: list[str] | None = None) -> dict:
    """Answer flexible questions with one read-only SELECT/WITH or PRAGMA table_info(table).

    Product discovery uses qualified SQL against items and optional product_details.
    Search names/categories with product_matches(i.name, ?) or product_matches(d.category, ?).
    It normalizes case, spaces, underscores, hyphens and common plurals; it matches whole words/phrases.
    Simple name/category equality, IN and LIKE filters are normalized locally, without changing stored labels.
    Keep item codes, SKUs, customer IDs and financial fields exact. Ambiguous products need clarification.
    Count all matching products before displaying a page. Use stable ordering and LIMIT/OFFSET.
    Include out-of-stock products unless explicitly filtered. Preserve scope on next-page requests.
    Use effective price_history for current prices; overlapping prices are unknown, never guessed.

    Join/filter/group permitted base tables instead of needing a tool per question.
    Inspect columns with PRAGMA. No database views exist. External payment records are unavailable;
    store_credit_ledger records local credit grants and redemptions only.
    Invoice totals minus recorded refunds are not amounts due or proof of settlement.
    Use ? placeholders with parameters for user-provided values; never interpolate names into SQL.
    For requested admin visuals optionally pass chart={"type":"bar","title":"Revenue","x":"month","y":["revenue"]}.
    Use the chart data contract for all 15 types. Match x/y and optional group, size, end or label
    to distinct returned aliases. Use one currency per monetary chart and return it as metadata.
    Chart queries allow up to 1000 complete rows. Ordinary reads use SQL_MAX_ROWS.
    A chart_error leaves query rows usable. Correct the SQL or chart specification before retrying.
    Admins may use queries instead of query/chart/parameters. Each entry is
    {"query":"SELECT ...","parameters":[],"chart":{...},"title":"Optional dataset title"}.
    Omit chart for table reads. Batch independent reads for a dashboard or multi-part analysis.
    For a request covering specific chart types, use expected_chart_types to list all requested
    types, including every supported type when the user requests full coverage. Each chart still
    needs a model-authored query and specification. Check which types attached before answering.
    To export table results, set report to a descriptive title. Results keep query order and each reports its own errors.
    """
    if queries is None and report is None and expected_chart_types is None:
        return _execute_read(query, chart, parameters)

    from mcp_server.access import scope
    if (scope.get() or {}).get('mode') != 'admin':
        return {'ok': False, 'error': 'SQL batches, chart coverage and report exports require Admin mode.'}
    coverage = {}
    if expected_chart_types is not None:
        from mcp_server.charts import normalize_chart_types
        try:
            coverage['expected_chart_types'] = normalize_chart_types(expected_chart_types)
        except ValueError as exc:
            return {'ok': False, 'error': str(exc)}
    if report is not None and (not isinstance(report, str) or not report.strip() or len(report) > 160):
        return {'ok': False, 'error': 'Provide a report title of 1 to 160 characters.'}
    if queries is None:
        return {**_execute_read(query, chart, parameters, export=report is not None), **coverage}
    if any(value is not None for value in (query, chart, parameters)) or not isinstance(queries, list) or not queries:
        return {'ok': False, 'error': 'Provide either one query or a non-empty queries list.'}
    with closing(connect(readonly=True)) as conn:
        _prepare_connection(conn)
        conn.execute('BEGIN')
        results = []
        for read in queries:
            if (not isinstance(read, dict) or 'query' not in read
                    or set(read) - {'query', 'parameters', 'chart', 'title'}):
                results.append({'ok': False, 'error': 'Each entry needs query and may include parameters, chart and title.'})
                continue
            title = read.get('title')
            if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 160):
                results.append({'ok': False, 'error': 'Dataset titles must contain 1 to 160 characters.'})
                continue
            results.append(_execute_read(read['query'], read.get('chart'), read.get('parameters'),
                                         export=report is not None, connection=conn))
    return {'ok': True, 'results': results, 'query_count': len(results), **coverage}


def _prepare_connection(conn):
    register_product_search(conn)
    conn.execute('PRAGMA query_only=ON')
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1000000)
    conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 20000)
    conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 100)


def _execute_read(query, chart, parameters, *, export=False, connection=None):
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
    conn = connection or (customer_query_connection(access['customer_id'], staff=access['mode'] == 'cashier')
            if access and access['mode'] in ('customer', 'cashier') else connect(readonly=True))
    try:
        if connection is None:
            _prepare_connection(conn)
        deadline = time.monotonic() + 2
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.set_authorizer(authorize)
        original_query = query
        query = normalize_product_filters(query)
        repaired_aliases = False
        try:
            cursor = conn.execute(query, parameters or [])
        except sqlite3.OperationalError as exc:
            repaired = quote_reserved_aliases(query) if chart is not None and 'syntax error' in str(exc).lower() else query
            if repaired == query:
                raise
            cursor = conn.execute(repaired, parameters or [])
            repaired_aliases = True
        from mcp_server.charts import MAX_CHART_ROWS
        from context.reports import MAX_REPORT_ROWS
        row_limit = MAX_CHART_ROWS if chart is not None else MAX_REPORT_ROWS if export else settings.SQL_MAX_ROWS
        rows = cursor.fetchmany(row_limit + 1)
        if any(isinstance(value, float) and not math.isfinite(value) for row in rows for value in row):
            return {'ok':False,'error':'The query produced a non-finite number. Narrow the calculation or correct its inputs.'}
        result = {'ok': True, 'columns': [c[0] for c in cursor.description or []],
                'rows': [list(row) for row in rows[:row_limit]],
                'row_count': min(len(rows), row_limit),
                'truncated': len(rows) > row_limit,
                'coverage_note': 'row_count counts returned SQL rows only. SQL LIMIT and filters may exclude matches even when truncated is false. Count the full matching set separately before claiming completeness, absence or a store-wide total.'}
        if repaired_aliases:
            result['query_note'] = 'Quoted reserved SQL aliases locally. Use names such as series_label instead of group/order in future queries.'
        if query != original_query:
            result['product_search_note'] = 'Product name/category filters ignore case, separators and common plural endings. IDs and financial filters retain their original semantics.'
        if chart is not None:
            from mcp_server.charts import prepare_chart
            try:
                payload = prepare_chart(result['columns'],result['rows'],chart,result['truncated'])
                result['chart'] = {key:value for key,value in payload.items() if key != 'data'}
            except ValueError as exc:
                result['chart_error'] = str(exc)
        return result
    except sqlite3.Error as exc:
        return _sql_error_details(exc, access['mode'] if access else 'admin')
    finally:
        if connection is None:
            conn.close()
