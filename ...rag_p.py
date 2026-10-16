"""Validate declarative charts made only from executed query rows."""
import math
import re
from datetime import datetime

CHART_TYPES = ('bar','stacked_bar','normalized_bar','line','pie','scatter',
               'histogram','boxplot','heatmap','range')
MAX_CHART_ROWS = 1000
SERIES_TYPES = {'bar','stacked_bar','normalized_bar','line'}
# Older saved attachments and callers resolve to the retained implementation.
CHART_ALIASES = {'grouped_bar':'bar','horizontal_bar':'bar','donut':'pie',
                 'area':'line','step_line':'line','stacked_area':'stacked_bar',
                 'normalized_area':'normalized_bar','box_plot':'boxplot','heat_map':'heatmap'}


def canonical_chart_type(kind):
    if not isinstance(kind, str):
        return kind
    kind = kind.strip().lower().replace(' ', '_').replace('-', '_').removesuffix('_chart')
    return CHART_ALIASES.get(kind, kind)
SPEC_FIELDS = {'type','title','x','y','group','x_type','bins'}
CHART_GUIDE = """
Chart contract for execute_sql_query:
- Exactly ten types: bar, stacked_bar, normalized_bar, line, pie, scatter,
  histogram, boxplot, heatmap, range. Choose one suitable chart per analysis.
- x names a returned column; y lists numeric columns. Bars/lines accept 1-4 measures
  with the same unit, uniquely grouped by x. For grouped series use group (a different
  column from x) and exactly one y. Use bar for single and grouped comparisons.
- Stacked bars require nonnegative values. normalized_bar shows percentage shares
  and requires a positive total at every x. Do not mix currencies or units.
- Bars/lines/range support x_type categorical (default), numeric or temporal.
  Temporal axes accept ISO dates/times, YYYY-MM or YYYY; order periods in SQL.
- scatter uses numeric x and one numeric y; optional group provides category colors.
- histogram uses numeric x, y:[], optional bins integer 2-50. Supply complete raw
  observations. boxplot uses categorical x, one raw numeric y and optional group.
- heatmap uses categorical x and group as distinct dimensions, one numeric y,
  and one aggregated row per x/group pair.
- range uses y:[lower,upper], with lower <= upper, uniquely grouped by x.
- pie uses one nonnegative y and a positive total, at most 12 categories. For more
  categories use bars or accurately aggregate smaller categories into Other.
- Max 1000 complete rows, no null coordinates/measures, no truncated results,
  one currency per monetary chart. Include currency as a returned column.
- Up to six charts per response. No arbitrary code, URLs or raw Vega specifications.
  If a requested type is unavailable, explain and offer a retained chart suited to
  the data. Do not silently change the requested measure or claim an absent chart.
"""


def numeric(value):
    if value is None or isinstance(value, bool):
        raise ValueError('Chart measures must be finite numbers, without null values.')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Chart measures must be numeric.') from None
    if not math.isfinite(number):
        raise ValueError('Chart measures must be finite.')
    return number


def label(value):
    if value is None or isinstance(value,(dict,list,bool)):
        raise ValueError('Chart category values must be non-null text or numbers.')
    result = str(value)
    if not result.strip() or len(result)>200:
        raise ValueError('Chart category labels require 1-200 characters.')
    return result


def temporal(value, periods=False):
    if periods and isinstance(value,str):
        if re.fullmatch(r'\d{4}-\d{2}',value):
            value += '-01'
        elif re.fullmatch(r'\d{4}',value):
            value += '-01-01'
    if not isinstance(value,str) or (len(value)!=10 and (len(value)<16 or value[10] not in ('T',' '))):
        raise ValueError('Temporal coordinates require ISO date or date-time strings.')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise ValueError('Temporal coordinates require valid ISO dates or date-times.') from None
    return parsed


def prepare_chart(columns, rows, spec, truncated=False):
    if not isinstance(spec,dict) or set(spec)-SPEC_FIELDS:
        raise ValueError('Chart accepts only type, title, x, y, group, x_type and bins.')
    spec = dict(spec)
    spec['type'] = canonical_chart_type(spec.get('type'))
    if isinstance(spec.get('y'), str):
        spec['y'] = [spec['y']]
    if spec.get('group') == spec.get('x') and spec.get('type') in SERIES_TYPES | {'boxplot'}:
        # Grouping a category by itself adds no information and duplicates an axis.
        spec.pop('group', None)
    kind,x,ys=spec.get('type'),spec.get('x'),spec.get('y')
    if not isinstance(kind,str) or kind not in CHART_TYPES:
        raise ValueError('Unsupported chart type. Choose one of: '+', '.join(CHART_TYPES)+'.')
    if not all(isinstance(c,str) for c in columns) or len(columns)!=len(set(columns)):
        raise ValueError('Give every selected SQL column a unique text alias.')
    if not isinstance(x,str) or x not in columns or not isinstance(ys,list):
        raise ValueError('Chart x must name a returned column and y must be a list of columns.')
    count = 0 if kind == 'histogram' else 2 if kind == 'range' else None
    single = kind in ('pie','scatter','boxplot','heatmap')
    if (count is not None and len(ys)!=count) or (single and len(ys)!=1) or (count is None and not single and not 1<=len(ys)<=4):
        raise ValueError(f'{kind} has the wrong number of y columns. Check the chart contract.')
    if any(not isinstance(y,str) or y not in columns or y==x for y in ys) or len(ys)!=len(set(ys)):
        raise ValueError('Chart y columns must exist, be distinct, and differ from x.')
    allowed_extra = {'group'} if kind in SERIES_TYPES or kind in ('scatter','boxplot','heatmap') else {'bins'} if kind=='histogram' else set()
    grouped_series = kind in SERIES_TYPES and 'group' in spec
    if grouped_series and len(ys) != 1:
        raise ValueError('A series group requires exactly one y measure.')
    if set(spec).intersection({'group','bins'})-allowed_extra:
        raise ValueError('This chart type does not support one of the supplied optional fields.')
    required = {'group'} if kind=='heatmap' else set()
    if not required<=set(spec):
        raise ValueError(f'{kind} requires: '+', '.join(sorted(required))+'.')
    extras={k:spec[k] for k in ('group',) if k in spec}
    if any(not isinstance(v,str) or v not in columns for v in extras.values()) or len(set([x,*ys,*extras.values()]))!=len([x,*ys,*extras.values()]):
        raise ValueError('Chart coordinate columns must exist and be distinct.')
    default_axis='numeric' if kind in ('scatter','histogram') else 'categorical'
    axis=spec.get('x_type',default_axis)
    if not isinstance(axis,str) or axis not in ('categorical','numeric','temporal'):
        raise ValueError('x_type must be categorical, numeric or temporal.')
    fixed = kind in ('scatter','histogram','pie','heatmap','boxplot')
    if fixed and axis!=default_axis:
        raise ValueError(f'{kind} requires {default_axis} x coordinates.')
    bins=spec.get('bins',10)
    if kind=='histogram' and (type(bins) is not int or not 2<=bins<=50):
        raise ValueError('Histogram bins must be an integer from 2 to 50.')
    if truncated:
        raise ValueError('Query results were truncated. Aggregate or narrow the query before charting; distributions require complete observations.')
    if not rows or len(rows)>MAX_CHART_ROWS or any(len(r)!=len(columns) for r in rows):
        raise ValueError('Charts require 1-1000 complete rows. Aggregate or narrow the query.')
    if kind == 'pie' and len(rows)>12:
        raise ValueError('Pie charts support at most 12 categories; group smaller categories or use bars.')
    title=spec.get('title') or 'Query results'
    if not isinstance(title,str) or len(title)>160:
        raise ValueError('Chart title must be text of at most 160 characters.')
    currencies={r[columns.index('currency')] for r in rows} if 'currency' in columns else set()
    if currencies and (len(currencies)!=1 or any(not isinstance(c,str) or not c.strip() or len(c)>20 for c in currencies)):
        raise ValueError('Chart one currency at a time. Filter currency and return it as currency.')
    records,seen=[],set()
    nonnegative=kind in ('pie','stacked_bar','normalized_bar')
    for row in rows:
        raw=row[columns.index(x)]
        coordinate=numeric(raw) if axis=='numeric' else temporal(raw,periods=True).isoformat() if axis=='temporal' else label(raw)
        values={y:numeric(row[columns.index(y)]) for y in ys}
        record={x:coordinate,**values}
        if 'group' in extras:
            record[extras['group']]=label(row[columns.index(extras['group'])])
        key=(coordinate,record[extras['group']]) if kind=='heatmap' or grouped_series else coordinate
        if kind not in ('scatter','histogram','boxplot') and key in seen:
            raise ValueError('Group duplicate chart coordinates in SQL before charting. Normalize currency before grouping, including legacy NULL/blank values; use an outer CTE to group the resolved currency, not the raw source column. Do not sum averages or ratios to repair duplicates.')
        seen.add(key)
        if nonnegative and any(v<0 for v in values.values()):
            raise ValueError(f'{kind} requires nonnegative values.')
        if kind in ('stacked_bar','normalized_bar') and not math.isfinite(sum(values.values())):
            raise ValueError('Stacked totals overflow. Scale or narrow the measures.')
        if kind == 'normalized_bar' and not grouped_series and sum(values.values())<=0:
            raise ValueError('Percentage charts require a positive total for each x value.')
        if kind=='range' and values[ys[0]]>values[ys[1]]:
            raise ValueError('Range lower bound must not exceed upper bound.')
        records.append(record)
    if grouped_series and kind == 'normalized_bar':
        groups = {}
        for record in records:
            groups[record[x]] = groups.get(record[x], 0) + record[ys[0]]
        if any(not math.isfinite(total) or total <= 0 for total in groups.values()):
            raise ValueError('Percentage charts require a finite positive total for each x value.')
    if kind == 'pie' and sum(r[ys[0]] for r in records)<=0:
        raise ValueError(f'{kind} needs a positive total.')
    totals=[sum(r[y] for r in records) for y in ys]
    if any(not math.isfinite(v) for v in totals):
        raise ValueError('Chart totals overflow. Scale or narrow the measures.')
    return {'type':kind,'title':title,'x':x,'y':ys,'currency':next(iter(currencies),None),
            'x_type':axis,**extras,**({'bins':bins} if kind=='histogram' else {}),'data':records}


def chart_from_result(result):
    metadata=result['chart']
    return prepare_chart(result['columns'],result['rows'],
                         {k:v for k,v in metadata.items() if k in SPEC_FIELDS},result.get('truncated',False))

































"""SQLite parser/authorizer is the security boundary, not a keyword regex."""
import math
import re
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
    """Read metadata only, scoped like the query tool; no business rows or cache."""
    conn = connect(readonly=True)
    try:
        tables = sorted(TABLES - {'json_each'} - ({'billing_audit'} if mode != 'admin' else set()))
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
        lines.append('Aggregate each one-to-many side before combining totals. Missing columns are unavailable; do not invent them.')
        return '\n'.join(lines)
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
    Types: bar,stacked_bar,normalized_bar,line,
    pie,scatter,histogram,boxplot,heatmap,range. x/y and optional group must be returned column aliases.
    Histogram: numeric x,y:[],bins 2-50. Heatmap: x,group,y:[measure]. Range: y:[low,high].
    Other types accept the column counts in the chart contract. Optional x_type: categorical,numeric,temporal.
    Standard bars/lines also accept group with one y, returning one row per x/group pair.
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
        row_limit = MAX_CHART_ROWS if chart is not None else settings.SQL_MAX_ROWS
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
        if chart is not None:
            from mcp_server.charts import prepare_chart
            try:
                payload = prepare_chart(result['columns'],result['rows'],chart,result['truncated'])
                result['chart'] = {key:value for key,value in payload.items() if key != 'data'}
            except ValueError as exc:
                result['chart_error'] = str(exc)
        return result
    except sqlite3.Error as exc:
        result = {'ok': False, 'error': f'Query rejected or failed: {exc}'}
        if any(term in str(exc).lower() for term in ('no such column', 'ambiguous column', 'no such table')):
            result['schema_hint'] = schema_hint(access['mode'] if access else 'admin')
        return result
    finally:
        conn.close()


























"""Render validated charts with the existing Altair/Streamlit stack."""
import logging
import altair as alt
import streamlit as st


def build_chart(chart):
    from mcp_server.charts import CHART_TYPES, canonical_chart_type
    kind,x,ys=chart['type'],chart['x'],chart['y']
    kind = canonical_chart_type(kind)
    if kind not in CHART_TYPES:
        raise ValueError('Unsupported saved chart type.')
    axis=chart.get('x_type','numeric' if kind in ('scatter','histogram') else 'categorical')
    xtype={'categorical':'O','numeric':'Q','temporal':'T'}[axis]
    xsort=alt.SortField(field='position',order='ascending')
    rows=[]
    for i,r in enumerate(chart['data']):
        row={'x_value':r[x],'position':i}
        row.update({f'v{j}':r[y] for j,y in enumerate(ys)})
        for source,target in [('group','category')]:
            if source in chart:
                row[target]=r[chart[source]]
        rows.append(row)
    def base(data=rows):
        return alt.Chart(alt.Data(values=data))
    measure=chart.get('currency') or (ys[0] if len(ys)==1 else 'Value')
    xc=alt.X('x_value:'+xtype,title=x,**({'sort':xsort} if xtype=='O' else {}))
    tip=[alt.Tooltip('x_value:'+xtype,title=x)]
    tip += [alt.Tooltip(f'v{i}:Q',title=y+(f" ({chart['currency']})" if chart.get('currency') else ''),format=',.2f') for i,y in enumerate(ys)]
    if 'group' in chart:
        tip.append(alt.Tooltip('category:N',title=chart['group']))
    if kind == 'pie':
        return base().mark_arc().encode(
            theta=alt.Theta('v0:Q'),color=alt.Color('x_value:N',title=x),tooltip=tip).properties(height=320)
    if kind == 'scatter':
        c=base().mark_circle(size=70,opacity=0.75).encode(x=xc,y=alt.Y('v0:Q',title=measure),tooltip=tip)
        if 'group' in chart:
            c=c.encode(color=alt.Color('category:N',title=chart['group']))
        return c.properties(height=320)
    if kind=='histogram':
        label=x+(f" ({chart['currency']})" if chart.get('currency') else '')
        return base().mark_bar().encode(x=alt.X('x_value:Q',bin=alt.Bin(maxbins=chart.get('bins',10)),title=label),
            y=alt.Y('count():Q',title='Count'),tooltip=[alt.Tooltip('count():Q',title='Count')]).properties(height=320)
    if kind=='boxplot':
        c=base().mark_boxplot(extent=1.5).encode(x=xc,y=alt.Y('v0:Q',title=measure))
        if 'group' in chart:
            c=c.encode(color=alt.Color('category:N',title=chart['group']),xOffset='category:N')
        return c.properties(height=320)
    if kind=='heatmap':
        return base().mark_rect().encode(x=xc,y=alt.Y('category:N',title=chart['group']),
            color=alt.Color('v0:Q',title=measure),tooltip=tip).properties(height=320)
    if kind=='range':
        return base().mark_rule(strokeWidth=8).encode(x=xc,y=alt.Y('v0:Q',title=measure),y2='v1:Q',tooltip=tip).properties(height=320)
    long=[{'x_value':r['x_value'],'value':r[f'v{j}'],'series':r.get('category',y),'position':r['position']}
          for r in rows for j,y in enumerate(ys)]
    b=base(long)
    if kind in ('bar','stacked_bar','normalized_bar'):
        c=b.mark_bar()
    elif kind == 'line':
        c=b.mark_line(point=True)
    else:
        raise ValueError('Unsupported chart type.')
    stack='normalize' if kind.startswith('normalized') else 'zero' if kind.startswith('stacked') else None
    value_axis=alt.Axis(format='%') if stack=='normalize' else alt.Axis()
    title='Share' if stack=='normalize' else measure
    series_title=chart.get('group','Measure')
    c=c.encode(color=alt.Color('series:N',title=series_title),tooltip=[alt.Tooltip('x_value:'+xtype,title=x),
        alt.Tooltip('series:N',title=series_title),alt.Tooltip('value:Q',title=measure,format=',.2f')])
    c=c.encode(x=xc,y=alt.Y('value:Q',title=title,stack=stack,axis=value_axis))
    if kind == 'bar' and (len(ys)>1 or 'group' in chart):
        c=c.encode(xOffset='series:N')
    return c.properties(height=320)


def render_chart(chart):
    try:
        result=build_chart(chart)
        result.to_dict(validate=True)
        st.subheader(chart['title'])
        st.altair_chart(result,use_container_width=True)
    except Exception:
        logging.getLogger(__name__).exception('Unable to render saved chart')
        st.warning('This saved chart cannot be displayed. Ask the agent to generate it again with a supported type and valid data.')
        return False
    st.caption('Based on the saved query result' + (f" ({chart['currency']})" if chart.get('currency') else '') + '. Refresh the question for current figures.')
    with st.expander('View chart data'):
        st.dataframe(chart['data'],hide_index=True,use_container_width=True)
    return True

































from mcp_server.charts import CHART_GUIDE

TASK_POLICY = """
SHARED TOOL REASONING AND DATA RULES
These rules apply in both modes. Mode-specific instructions restrict which tools/actions are allowed.
Do not expose a chain of thought, internal plan or SQL script in the final customer-facing answer.

A. Understand the requested outcome
Identify whether the user wants discovery, a recommendation, a comparison, a factual record lookup,
a calculation, a chart, an account/order change or an after-sale action. Some messages need several.
Determine scope: associated customer versus store, whole product range versus filtered matches, dates,
currency, quantity and budget scope. Reuse established context, but explicit corrections override it.
Separate necessary clarification from optional preferences. Do useful reads before asking when safe.
Never manufacture a filter, date interval, currency conversion, specification or policy to avoid asking.
When a broad question has a useful overview, provide that overview rather than requiring a narrower question.
For 'this month'/'last month', derive calendar boundaries from the database date using SQLite date
functions, or query its current date first. Label the actual interval. Tool date filters use inclusive order_date.
A refund grouped by order_date is not a refund-processing-date trend; a processing date is not stored.

B. Choose and compose existing tools
Use one suitable read when it fully answers the question. Combine several existing tools when needed.
Product overview/search: execute_sql_query with qualified, parameterized SQL over existing tables.
Comparisons: compare_products. Cheaper alternatives: find_cheaper_alternatives.
Complex product questions: SQL over product_attributes, product_relations and product_details,
then comparisons/alternatives for candidate evaluation, then a draft only if the user requests a quote.
Customer/order/invoice facts: scoped SQL followed by an appropriate supported action when authorized.
Admin invoice/cost summaries: billing_analysis. Admin evidence of inconsistencies: billing_anomalies.
Admin graphs: execute_sql_query with chart metadata, using actual eligible rows and correct measures.
Never assume the application needs another tool for each new wording of a question.
Never fabricate a tool name, SQL write, mutation action, parameter or external service capability.
Do not call every tool for every message. Reuse results within the turn and avoid duplicate reads.
A multi-step read workflow does not authorize the writes it might eventually enable.

C. Read schema safely and write correct SQL
Use the declared schema and actual tool schemas. Inspect PRAGMA table_info(table) when a needed
column is unknown. Do not guess synonyms such as price, stock, payment_status or created_at.
Only read allowed BASE TABLES with SELECT/WITH or permitted PRAGMA; no database views exist.
Use ? placeholders and the parameters list for user names, identifiers, dates, search terms and bounds.
Never interpolate user text into SQL, table names, sort expressions or SQL fragments.
Qualify every joined column, including SELECT, WHERE, GROUP BY and ORDER BY.
Use explicit aliases: items i LEFT JOIN product_details d ON d.item_code=i.item_code;
select i.item_code, i.name, i.sellingprice, i.availablequantity, d.category rather than bare item_code.
LEFT JOIN optional metadata so missing product_details/order_details does not silently delete records.
Preserve the intended unit of aggregation. COUNT(*) after a one-to-many join may count lines, not products.
Use COUNT(DISTINCT i.item_code) for distinct products when joining several attribute rows.
Select needed columns, not indiscriminate SELECT *. Avoid outputting unnecessary customer details.
Use grouping/subqueries/CTEs to answer totals in the database, not summing a truncated displayed sample.

D. Be explicit about coverage
SQL row_count is returned row count, not the count of all matching products, customers or invoices.
truncated=false only means the transport cap was not reached. A SQL LIMIT still excludes rows.
Before claiming 'all', 'none', 'only', 'cheapest', 'highest' or a full total, establish the matching scope.
For product discovery, query category totals and a matching COUNT using the SAME filters; evaluate ranking across the whole matching set before limiting display.
A top-N query may be valid, but label it top-N. Do not call five examples the full product range.
For a page state the range and total. If the result is empty beyond the final page, the product range is
not necessarily empty; distinguish an exhausted page from zero matching products.
Do not present truncated datasets as full distributions or combine chunks without preserving the scope.
When a task cannot be completed within available tools/iterations, describe completed and missing parts
accurately. Never manufacture the unqueried remainder.

E. Product queries and follow-ups
Use execute_sql_query(query=..., parameters=[...]) against items and optional product_details.
Whole-store summary example:
SELECT COALESCE(d.category,'Uncategorised') AS category, COUNT(*) AS products,
SUM(CASE WHEN i.availablequantity>0 THEN 1 ELSE 0 END) AS in_stock_products
FROM items i LEFT JOIN product_details d ON d.item_code=i.item_code
GROUP BY COALESCE(d.category,'Uncategorised') ORDER BY category;
Sum all returned category counts for the total only when that grouped result is complete.
If truncated, obtain a separate COUNT(*) from items and paginate categories as necessary.
For a nonempty product page, return COUNT(*) OVER() AS matching_products with the same filtered
product rows and stable ORDER BY i.name, i.item_code LIMIT ? OFFSET ?. The window count is computed
before LIMIT, so a separate count call is unnecessary. Use a one-row-per-product source before counting.
If a requested page is empty, query a matching COUNT to distinguish no matches from an exhausted page.
Show at most ten products per page. Keep filters and ordering in recent conversation context;
advance the offset by the page size for 'next', and stop once the matching count is reached.
After a summary, start the detailed list at offset zero. Changed filters restart at offset zero.
Use bound values for search/category/price filters. Escape LIKE wildcards when literal text is intended.
Default scope includes out-of-stock products. Only filter stock when explicitly requested.
For current prices use the effective-price rules below before sorting, filtering or showing prices.
A price bound is CURRENT UNIT merchandise price; translate quantity budgets appropriately.
Ambiguous prices are unknown, not zero; exclude them from price-bounded results and explain exclusions.
Use attributes, relationships and categories for semantic requirements, compatibility and bundles.

F. Pricing, features and relationships
Current price: price_history effective_from<=CURRENT_DATE and effective_to IS NULL or >=CURRENT_DATE.
Count active price rows per item: exactly one supplies unit_price; zero falls back to sellingprice;
more than one is ambiguous and requires staff correction. Never resolve overlap by LIMIT, MAX or MIN.
price_history dates are inclusive. Draft quotes use order_date. Issued invoices use invoice_lines.
Read product_attributes text_value/number_value/unit. Structured attributes override same-name
features_json keys case-insensitively. If a key is absent, primitive JSON features may supply it.
Missing/null features are unknown, not false or zero. Respect numeric units, exact-match tool requirements
and the distinction between a threshold request and an exact stored value. Use SQL for thresholds.
Do not convert units or currencies without a verified conversion basis. Do not round away budget breaches.
Product relations are directional substitute/compatible/accessory links; read the correct direction.
A recorded relationship supports that recorded fact, not every imagined device/environment compatibility.
Check candidate stock and current price before recommending or quoting. Do not reveal baseprice in customer mode.

G. Financial definitions and avoiding duplicated totals
Issued invoices have order_invoice IS NOT NULL. Drafts/cancelled drafts are not billed sales.
For issued-order totals read orders o LEFT JOIN order_details d ON d.order_id=o.order_id and filter
COALESCE(d.status,'active')<>'cancelled'. Keep the user's requested customer/date/order filters.
Currency is COALESCE(d.currency,(SELECT value FROM billing_config WHERE key='currency'),'USD').
Group or filter by currency; never sum money in unlike currencies.
Sum subtotal, charged_price, tax_amount, shipping_amount, discount_applied and refund_amount at the
ORDER level. Joining invoice_lines/order_requests/order_fulfilled before summing duplicates order totals.
If line and order summaries are needed, aggregate each separately then join one-row-per-order results.
Product units/revenue come from invoice_lines joined to eligible issued orders and items.
Line_total is merchandise before order-level discount, tax, shipping and refund allocation.
Do not silently assign an entire order refund/discount to every line or invent per-line allocations.
Charged invoice total = subtotal minus applied discount plus tax plus shipping, as the tools calculate.
Recorded refund-adjusted invoice amount = charged_price minus COALESCE(refund_amount,0), rounded
for currency reporting. It is not cash collected or an outstanding amount due.
Payments and credit notes are not stored. No paid/unpaid status, actual balance due, overdue debt,
customer credit balance, provider refund timing or payment reconciliation can be established here.
Order due dates do not supply those missing payment facts. Refer payment reconciliation to store staff.
Current baseprice supports admin estimated procurement cost, not historical cost or operating profit.
State which margin/cost measure is being reported and its limitations. Never treat it as audited net profit.
An anomaly is evidence for review, not proof of fraud or a license to automatically change/refund records.
billing_audit is retained internal change history and is admin-only.

H. Tool failures, recovery and accurate completion
Read ok/error and every relevant warning before composing the answer.
Correct an ambiguous-column error with qualified SQL. Correct a chart error using returned requirements.
Do not repeat an unchanged invalid read or exhaust iterations on the same impossible operation.
A failed read does not mean there are no rows. An inaccessible account does not mean it does not exist.
A failed mutation does not authorize another mutation, alternative customer ID or weaker confirmation.
For transport failures/timeouts after a possible write, inspect stored state before deciding whether
it completed. Do not blindly replay request_order/place_order/refund or create a duplicate customer.
Reuse established order IDs for safe draft retry when appropriate. The app's request-id retry is not
a suggestion to invent new business actions. Never claim success until verified by a successful tool/state read.
If only part of a multi-request task succeeded, identify that part and the unresolved part plainly.
Do not expose raw exceptions, internal JSON, stack traces or secrets in the visible answer.

I. Admin charts and visual analytics (ADMIN MODE ONLY)
For graph/chart/plot/pictorial/dashboard requests, call execute_sql_query with its chart argument.
Customer mode cannot create admin chart artifacts; do not attempt to bypass that restriction.
Text-only questions need no chart. A chart is an attachment from successful tool output, not a URL
or hand-drawn ASCII/Markdown picture. Never generate Python/JavaScript/Mermaid or raw Vega as a substitute.
Choose a supported chart suited to the requested relationship and actual stored data.
Simple comparison: bars. Time trend: line. Parts of a positive whole: pie with limited categories.
Distribution: histogram/boxplot with raw observations. Relationship: scatter with numeric axes.
Two-dimensional categories: heatmap. Minimum/maximum intervals: range. Rankings: sorted bars.
Use the detailed chart contract below for all supported types, measures and field restrictions.
Include returned aliases for x/y/group. Charts must contain at most 1000 complete rows.
Use at most six charts per turn; offer remaining views in a later turn.
Include currency as a column named currency for monetary charts and filter to ONE currency per chart.
When multiple currencies exist and none was requested, inspect them and ask or create clearly separate
currency-specific charts. Different definitions/units may also require separate charts.
Sort periods chronologically in SQL. Preserve raw observations for distributions; aggregate duplicate
coordinates for summaries. A selected top-N plot is titled top-N, not all-store distribution.
For a pie over more than twelve categories, use a suitable bar chart or an accurately labelled top-N
plus a SQL-computed Other group, with the full denominator. Never silently discard categories.
For 'show that as a chart', reuse the previous measure, filters, currency and date scope with fresh data.
No rows means no data. Do not fabricate a zero series or invent observations to make a chart valid.
chart_error means no chart was produced. Repair a suitable query/spec or explain and offer an alternative.
When the tool confirms a chart artifact, finish with a brief plain-English explanation of scope,
measure and useful findings; do not duplicate the entire plotted dataset in the reply.
Chart attachments and PDFs are handled by the UI; do not insert imaginary download links.
""" + CHART_GUIDE


# Customers cannot use chart artifacts; omit their entire admin-only contract.
CUSTOMER_TASK_POLICY = TASK_POLICY.split("I. Admin charts and visual analytics", 1)[0] + (
    "Customer mode cannot create charts or call admin analytics tools. "
    "Explain that store-wide visual reports require Admin mode.\n")
