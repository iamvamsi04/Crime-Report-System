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
    """Materialize only public products and associated customer rows in an isolated DB.

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





















"""Validate declarative charts made only from executed query rows."""
import math
from datetime import datetime

CHART_TYPES = ('bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar',
    'line','step_line','area','stacked_area','normalized_area','pie','donut','scatter',
    'bubble','histogram','boxplot','heatmap','waterfall','range','combo','timeline',
    'candlestick','pareto','funnel','treemap')
MAX_CHART_ROWS = 1000
SPEC_FIELDS = {'type','title','x','y','group','size','end','x_type','bins'}
CHART_GUIDE = """
Chart contract for execute_sql_query:
- Supported type: bar, grouped_bar, horizontal_bar, stacked_bar, normalized_bar, line,
  step_line, area, stacked_area, normalized_area, pie, donut, scatter, bubble, histogram,
  boxplot, heatmap, waterfall, range, combo, timeline, candlestick, pareto, funnel, treemap.
- x names a query column. y lists numeric columns. Standard bars/lines/areas accept 1-4
  measures, uniquely grouped by x. Stacked/normalized charts require nonnegative measures;
  normalized charts need a positive total for each x. Multiple measures must share a unit.
- x_type optionally selects categorical (default), numeric or temporal for standard
  bars/lines/areas, scatter, bubble, range or combo. Order trends in SQL, never lexically sort dates.
- scatter/bubble use numeric x and one numeric y. bubble also needs size, a nonnegative numeric
  column with some positive values. Optional group gives color categories for scatter/bubble/boxplot.
- histogram uses numeric x and y:[], optional bins integer 2-50. Supply raw observations, not
  already aggregated counts. boxplot uses categorical x and one raw numeric y; repeated x is required
  for meaningful distributions. Truncated samples must never stand in for the full distribution.
- heatmap uses x and group as its two categories and one numeric y, one row per x/group pair.
- range uses y:[lower,upper], lower<=upper. combo uses y:[bar_measure,line_measure] and independent,
  explicitly labelled axes. Do not mix currencies. waterfall uses one signed y change per ordered
  stage; do not include a total row, because cumulative totals are calculated from those changes.
- timeline uses x as an ISO start date/time, end as an ISO end date/time, group as the task label,
  and y:[]. End must not precede start. Plot only dates actually stored, not inferred delivery promises.
- candlestick uses y:[open,high,low,close]; low<=open/close<=high, one row per x period.
- pie/donut/treemap/pareto/funnel use one nonnegative y and a positive total. Pie/donut support
  at most 12 categories, treemap at most 50. Pareto sorts descending and adds cumulative percent.
  Funnel stages must be ordered by the query and nonincreasing; otherwise use horizontal bars.
- Max 1000 rows, no null coordinates/measures, no truncated results, one currency per monetary chart.
  No arbitrary code, URLs, external images or raw Vega specifications. Up to six charts per response.
- If a requested chart is unsupported or the data are unsuitable, say why and offer a supported
  alternative. Do not claim it was rendered. Ask for missing scope or measures instead of inventing data.
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


def temporal(value):
    if not isinstance(value,str) or (len(value)!=10 and (len(value)<16 or value[10] not in ('T',' '))):
        raise ValueError('Temporal coordinates require ISO date or date-time strings.')
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:
        raise ValueError('Temporal coordinates require valid ISO dates or date-times.') from None
    return parsed


def prepare_chart(columns, rows, spec, truncated=False):
    if not isinstance(spec,dict) or set(spec)-SPEC_FIELDS:
        raise ValueError('Chart accepts only type, title, x, y, group, size, end, x_type and bins.')
    kind,x,ys=spec.get('type'),spec.get('x'),spec.get('y')
    if kind not in CHART_TYPES:
        raise ValueError('Unsupported chart type. Choose one of: '+', '.join(CHART_TYPES)+'.')
    if not all(isinstance(c,str) for c in columns) or len(columns)!=len(set(columns)):
        raise ValueError('Give every selected SQL column a unique text alias.')
    if not isinstance(x,str) or x not in columns or not isinstance(ys,list):
        raise ValueError('Chart x must name a returned column and y must be a list of columns.')
    count = 0 if kind in ('histogram','timeline') else 2 if kind in ('range','combo') else 4 if kind=='candlestick' else None
    single = kind in ('pie','donut','scatter','bubble','boxplot','heatmap','waterfall','pareto','funnel','treemap')
    if (count is not None and len(ys)!=count) or (single and len(ys)!=1) or (count is None and not single and not 1<=len(ys)<=4):
        raise ValueError(f'{kind} has the wrong number of y columns. Check the chart contract.')
    if any(not isinstance(y,str) or y not in columns or y==x for y in ys) or len(ys)!=len(set(ys)):
        raise ValueError('Chart y columns must exist, be distinct, and differ from x.')
    allowed_extra = {'group'} if kind in ('scatter','boxplot') else {'group','size'} if kind=='bubble' else {'group'} if kind=='heatmap' else {'group','end'} if kind=='timeline' else {'bins'} if kind=='histogram' else set()
    if set(spec).intersection({'group','size','end','bins'})-allowed_extra:
        raise ValueError('This chart type does not support one of the supplied optional fields.')
    required = {'size'} if kind=='bubble' else {'group'} if kind=='heatmap' else {'group','end'} if kind=='timeline' else set()
    if not required<=set(spec):
        raise ValueError(f'{kind} requires: '+', '.join(sorted(required))+'.')
    extras={k:spec[k] for k in ('group','size','end') if k in spec}
    if any(not isinstance(v,str) or v not in columns for v in extras.values()) or len(set([x,*ys,*extras.values()]))!=len([x,*ys,*extras.values()]):
        raise ValueError('Chart coordinate columns must exist and be distinct.')
    default_axis='numeric' if kind in ('scatter','bubble','histogram') else 'temporal' if kind=='timeline' else 'categorical'
    axis=spec.get('x_type',default_axis)
    if axis not in ('categorical','numeric','temporal'):
        raise ValueError('x_type must be categorical, numeric or temporal.')
    fixed = kind in ('scatter','bubble','histogram','timeline','pie','donut','heatmap','boxplot','waterfall','pareto','funnel','treemap','candlestick')
    if fixed and axis!=default_axis:
        raise ValueError(f'{kind} requires {default_axis} x coordinates.')
    bins=spec.get('bins',10)
    if kind=='histogram' and (type(bins) is not int or not 2<=bins<=50):
        raise ValueError('Histogram bins must be an integer from 2 to 50.')
    if truncated:
        raise ValueError('Query results were truncated. Aggregate or narrow the query before charting; distributions require complete observations.')
    if not rows or len(rows)>MAX_CHART_ROWS or any(len(r)!=len(columns) for r in rows):
        raise ValueError('Charts require 1-1000 complete rows. Aggregate or narrow the query.')
    if kind in ('pie','donut') and len(rows)>12:
        raise ValueError('Pie and donut charts support at most 12 categories; group smaller categories or use bars.')
    if kind=='treemap' and len(rows)>50:
        raise ValueError('Treemap supports at most 50 categories.')
    title=spec.get('title') or 'Query results'
    if not isinstance(title,str) or len(title)>160:
        raise ValueError('Chart title must be text of at most 160 characters.')
    currencies={r[columns.index('currency')] for r in rows} if 'currency' in columns else set()
    if currencies and (len(currencies)!=1 or any(not isinstance(c,str) or not c.strip() or len(c)>20 for c in currencies)):
        raise ValueError('Chart one currency at a time. Filter currency and return it as currency.')
    records,seen=[],set()
    nonnegative=kind in ('pie','donut','stacked_bar','normalized_bar','stacked_area','normalized_area','pareto','funnel','treemap')
    for row in rows:
        raw=row[columns.index(x)]
        coordinate=numeric(raw) if axis=='numeric' else temporal(raw).isoformat() if axis=='temporal' else label(raw)
        values={y:numeric(row[columns.index(y)]) for y in ys}
        record={x:coordinate,**values}
        if 'group' in extras:
            record[extras['group']]=label(row[columns.index(extras['group'])])
        key=(coordinate,record[extras['group']]) if kind=='heatmap' else coordinate
        if kind not in ('scatter','bubble','histogram','boxplot','timeline') and key in seen:
            raise ValueError('Group duplicate chart coordinates in SQL before charting.')
        seen.add(key)
        if nonnegative and any(v<0 for v in values.values()):
            raise ValueError(f'{kind} requires nonnegative values.')
        if kind in ('stacked_bar','stacked_area','normalized_bar','normalized_area') and not math.isfinite(sum(values.values())):
            raise ValueError('Stacked totals overflow. Scale or narrow the measures.')
        if kind in ('normalized_bar','normalized_area') and sum(values.values())<=0:
            raise ValueError('Percentage charts require a positive total for each x value.')
        if kind=='range' and values[ys[0]]>values[ys[1]]:
            raise ValueError('Range lower bound must not exceed upper bound.')
        if kind=='candlestick' and not values[ys[2]]<=min(values[ys[0]],values[ys[3]])<=max(values[ys[0]],values[ys[3]])<=values[ys[1]]:
            raise ValueError('Candlestick requires low <= open/close <= high.')
        if kind=='bubble':
            record[extras['size']]=numeric(row[columns.index(extras['size'])])
            if record[extras['size']]<0:
                raise ValueError('Bubble sizes cannot be negative.')
        if kind=='timeline':
            finish=temporal(row[columns.index(extras['end'])])
            try:
                reversed_interval=finish<temporal(raw)
            except TypeError:
                raise ValueError('Timeline start and end must use consistent timezone information.') from None
            if reversed_interval:
                raise ValueError('Timeline end cannot precede start.')
            record[extras['end']]=finish.isoformat()
        records.append(record)
    if kind in ('pie','donut','pareto','funnel','treemap') and sum(r[ys[0]] for r in records)<=0:
        raise ValueError(f'{kind} needs a positive total.')
    if kind=='bubble' and not any(r[extras['size']]>0 for r in records):
        raise ValueError('At least one bubble must have a positive size.')
    if kind=='funnel' and any(b[ys[0]]>a[ys[0]] for a,b in zip(records,records[1:])):
        raise ValueError('Funnel values must be nonincreasing in stage order; otherwise use bars.')
    totals=[sum(r[y] for r in records) for y in ys]
    if any(not math.isfinite(v) for v in totals):
        raise ValueError('Chart totals overflow. Scale or narrow the measures.')
    return {'type':kind,'title':title,'x':x,'y':ys,'currency':next(iter(currencies),None),
            'x_type':axis,**extras,**({'bins':bins} if kind=='histogram' else {}),'data':records}


def chart_from_result(result):
    metadata=result['chart']
    return prepare_chart(result['columns'],result['rows'],
                         {k:v for k,v in metadata.items() if k in SPEC_FIELDS},result.get('truncated',False))


















from fastmcp import FastMCP
from mcp_server.access import scoped_tool
from mcp_server.tools.database import execute_sql_query
from mcp_server.tools.billing_tools import request_order, place_order, apply_promotion, process_refund, download_invoice
from mcp_server.tools.analytics_tools import billing_analysis, billing_anomalies
from mcp_server.tools.operations_tools import manage_customer, manage_draft
from mcp_server.tools.shopping_tools import compare_products, find_cheaper_alternatives

mcp = FastMCP("Billing tools")
TOOLS = (execute_sql_query, request_order, place_order, apply_promotion, process_refund, download_invoice,
         billing_analysis, billing_anomalies, manage_customer, manage_draft,
         compare_products, find_cheaper_alternatives)
for tool in TOOLS:
    mcp.tool(scoped_tool(tool))

if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)











































"""Render validated charts with the existing Altair/Streamlit stack."""
import logging
import altair as alt
import streamlit as st


def build_chart(chart):
    from mcp_server.charts import CHART_TYPES
    kind,x,ys=chart['type'],chart['x'],chart['y']
    if kind not in CHART_TYPES:
        raise ValueError('Unsupported saved chart type.')
    axis=chart.get('x_type','numeric' if kind in ('scatter','bubble','histogram') else 'temporal' if kind=='timeline' else 'categorical')
    xtype={'categorical':'O','numeric':'Q','temporal':'T'}[axis]
    xsort=alt.SortField(field='position',order='ascending')
    rows=[]
    for i,r in enumerate(chart['data']):
        row={'x_value':r[x],'position':i}
        row.update({f'v{j}':r[y] for j,y in enumerate(ys)})
        for source,target in [('group','category'),('size','size_value'),('end','end_value')]:
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
    if kind in ('pie','donut'):
        return base().mark_arc(innerRadius=50 if kind=='donut' else 0).encode(
            theta=alt.Theta('v0:Q'),color=alt.Color('x_value:N',title=x),tooltip=tip).properties(height=320)
    if kind in ('scatter','bubble'):
        c=base().mark_circle(size=70,opacity=0.75).encode(x=xc,y=alt.Y('v0:Q',title=measure),tooltip=tip)
        if kind=='bubble':
            c=c.encode(size=alt.Size('size_value:Q',title=chart['size'],scale=alt.Scale(range=[0,1600])),
                tooltip=tip+[alt.Tooltip('size_value:Q',title=chart['size'])])
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
    if kind=='combo':
        bars=base().mark_bar(opacity=0.55,color='#3c78a8').encode(x=xc,y=alt.Y('v0:Q',title=ys[0]),tooltip=tip)
        line=base().mark_line(point=True,color='#d97820').encode(x=xc,y=alt.Y('v1:Q',title=ys[1]),tooltip=tip)
        return alt.layer(bars,line).resolve_scale(y='independent').properties(height=320)
    if kind=='timeline':
        return base().mark_bar().encode(x=alt.X('x_value:T',title=x),x2='end_value:T',
            y=alt.Y('category:N',title=chart['group']),color=alt.Color('category:N',legend=None),
            tooltip=tip+[alt.Tooltip('end_value:T',title=chart['end'])]).properties(height=320)
    if kind=='candlestick':
        rule=base().mark_rule().encode(x=xc,y=alt.Y('v2:Q',title=measure),y2='v1:Q',tooltip=tip)
        body=base().mark_bar(size=12).encode(x=xc,y=alt.Y('v0:Q',title=measure),y2='v3:Q',
            color=alt.condition('datum.v3 >= datum.v0',alt.value('#20854e'),alt.value('#b4473b')),tooltip=tip)
        return alt.layer(rule,body).properties(height=320)
    if kind=='waterfall':
        total=0
        for r in rows:
            r['start']=total
            total+=r['v0']
            r['end']=total
        return base().mark_bar().encode(x=xc,y=alt.Y('start:Q',title=measure),y2='end:Q',
            color=alt.condition('datum.v0 >= 0',alt.value('#20854e'),alt.value('#b4473b')),
            tooltip=tip+[alt.Tooltip('end:Q',title='Cumulative total')]).properties(height=320)
    if kind=='funnel':
        for r in rows:
            r['left']=-r['v0']/2
            r['right']=r['v0']/2
        return base().mark_bar().encode(x=alt.X('left:Q',axis=None,title=measure),x2='right:Q',
            y=alt.Y('x_value:O',title=x,sort=xsort),color=alt.Color('x_value:N',legend=None),tooltip=tip).properties(height=320)
    if kind=='pareto':
        rows.sort(key=lambda r:r['v0'],reverse=True)
        total=sum(r['v0'] for r in rows)
        running=0
        for i,r in enumerate(rows):
            running+=r['v0']
            r['fraction']=running/total
            r['position']=i
        bars=base().mark_bar(color='#3c78a8').encode(x=xc,y=alt.Y('v0:Q',title=measure),tooltip=tip)
        line=base().mark_line(point=True,color='#d97820').encode(x=xc,
            y=alt.Y('fraction:Q',title='Cumulative percent',axis=alt.Axis(format='%'),scale=alt.Scale(domain=[0,1])),
            tooltip=tip+[alt.Tooltip('fraction:Q',title='Cumulative percent',format='.1%')])
        return alt.layer(bars,line).resolve_scale(y='independent').properties(height=320)
    if kind=='treemap':
        tiles=[]
        def split(parts,left,bottom,width,height):
            if len(parts)==1:
                tiles.append({**parts[0],'left':left,'right':left+width,'bottom':bottom,'top':bottom+height,
                              'cx':left+width/2,'cy':bottom+height/2})
                return
            middle=len(parts)//2
            fraction=sum(r['v0'] for r in parts[:middle])/sum(r['v0'] for r in parts)
            if width>=height:
                split(parts[:middle],left,bottom,width*fraction,height)
                split(parts[middle:],left+width*fraction,bottom,width*(1-fraction),height)
            else:
                split(parts[:middle],left,bottom,width,height*fraction)
                split(parts[middle:],left,bottom+height*fraction,width,height*(1-fraction))
        split(sorted((r for r in rows if r['v0']>0),key=lambda r:r['v0'],reverse=True),0,0,1,1)
        rectangles=base(tiles).mark_rect(stroke='white').encode(x=alt.X('left:Q',axis=None),x2='right:Q',
            y=alt.Y('bottom:Q',axis=None),y2='top:Q',color=alt.Color('x_value:N',title=x),tooltip=tip)
        return rectangles.properties(height=320)
    long=[{'x_value':r['x_value'],'value':r[f'v{j}'],'series':y,'position':r['position']}
          for r in rows for j,y in enumerate(ys)]
    b=base(long)
    if kind in ('bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar'):
        c=b.mark_bar()
    elif kind in ('line','step_line'):
        c=b.mark_line(**({'interpolate':'step-after'} if kind=='step_line' else {}))
    elif kind in ('area','stacked_area','normalized_area'):
        c=b.mark_area(opacity=0.6 if kind=='area' else 1)
    else:
        raise ValueError('Unsupported chart type.')
    stack='normalize' if kind.startswith('normalized') else 'zero' if kind.startswith('stacked') else None
    value_axis=alt.Axis(format='%') if stack=='normalize' else alt.Axis()
    title='Share' if stack=='normalize' else measure
    c=c.encode(color=alt.Color('series:N',title='Measure'),tooltip=[alt.Tooltip('x_value:'+xtype,title=x),
        alt.Tooltip('series:N',title='Measure'),alt.Tooltip('value:Q',title=measure,format=',.2f')])
    if kind=='horizontal_bar':
        c=c.encode(y=alt.Y('x_value:'+xtype,title=x,**({'sort':xsort} if xtype=='O' else {})),x=alt.X('value:Q',title=title,stack=stack))
        if len(ys)>1:
            c=c.encode(yOffset='series:N')
    else:
        c=c.encode(x=xc,y=alt.Y('value:Q',title=title,stack=stack,axis=value_axis))
        if kind in ('bar','grouped_bar') and len(ys)>1:
            c=c.encode(xOffset='series:N')
    return c.properties(height=320)


def render_chart(chart):
    try:
        result=build_chart(chart)
        result.to_dict(validate=True)
    except Exception:
        logging.getLogger(__name__).exception('Unable to render saved chart')
        st.warning('This saved chart cannot be displayed. Ask the agent to generate it again with a supported type and valid data.')
        return False
    st.subheader(chart['title'])
    st.altair_chart(result,use_container_width=True)
    st.caption('Based on the saved query result' + (f" ({chart['currency']})" if chart.get('currency') else '') + '. Refresh the question for current figures.')
    with st.expander('View chart data'):
        st.dataframe(chart['data'],hide_index=True,use_container_width=True)
    return True











































import os
import sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import streamlit as st
from config import settings
from context.reply_text import plain_reply, error_reply

st.set_page_config(
    page_title="Billing Agent",
    page_icon="💳",
    layout="centered",
    initial_sidebar_state="collapsed",
)
API = os.getenv("API_URL", "http://127.0.0.1:8000").rstrip("/")
HEADERS = {"X-API-Key": settings.API_KEY}


@st.cache_resource
def api_client():
    return httpx.Client(timeout=settings.CHAT_TIMEOUT + 30)


def request(method, route, **kwargs):
    headers = dict(HEADERS)
    if st.session_state.get('mode') == 'Admin' and st.session_state.get('admin_key'):
        headers['X-Admin-Key'] = st.session_state.admin_key
    result = api_client().request(method, API + route, headers=headers, **kwargs)
    result.raise_for_status()
    return result


for key, value in {"messages": [], "conversation_id": None, "pending": None, 'admin_key': '', 'mode': 'Customer'}.items():
    if key not in st.session_state:
        st.session_state[key] = value


def reset_mode():
    st.session_state.messages = []
    st.session_state.conversation_id = None
    st.session_state.pending = None
    st.session_state.admin_key = ''
    st.session_state.admin_password = ''


def logout():
    reset_mode()
    st.session_state.mode = 'Customer'

with st.sidebar:
    st.subheader("Store assistant")
    st.radio('Mode', ['Customer', 'Admin'], key='mode', on_change=reset_mode)
    if st.session_state.mode == 'Admin':
        st.caption('Read-only analytics and records')
        if not st.session_state.admin_key:
            password = st.text_input('Admin key', type='password', key='admin_password')
            if st.button('Unlock admin mode'):
                try:
                    response = api_client().request('GET', API + '/admin/access',
                        headers={**HEADERS, 'X-Admin-Key': password})
                    response.raise_for_status()
                    st.session_state.admin_key = password
                    st.rerun()
                except httpx.HTTPError:
                    st.error('Admin access denied or not configured. Check BILLING_ADMIN_API_KEY on the API server.')
            st.info('Enter the separate admin key to view store-wide analytics and records.')
            st.stop()
        st.button('Log out of admin mode', on_click=logout)
    else:
        st.caption('Shopping and billing help')
    customer = st.text_input("Customer ID (optional)", placeholder="CUST001", disabled=bool(st.session_state.conversation_id))
    if st.button("New conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state.conversation_id = None
        st.session_state.pending = None
        st.rerun()
    resume = st.text_input("Resume conversation ID")
    if st.button("Resume", disabled=not resume):
        try:
            saved = request("GET", f"/conversations/{resume}").json()
            if saved.get('mode', 'customer') != st.session_state.mode.lower():
                st.error('Switch to the matching mode before resuming this conversation.')
                st.stop()
            st.session_state.conversation_id = saved["conversation_id"]
            st.session_state.messages = saved["messages"]
            st.session_state.pending = None
            st.rerun()
        except httpx.HTTPError as exc:
            st.error(f"Could not resume: {exc}")
    if st.session_state.conversation_id:
        st.caption("Conversation ID")
        st.code(st.session_state.conversation_id, language=None)
    try:
        health = request("GET", "/health").json()
        if health["model_configured"]:
            st.success("Azure OpenAI configured")
            st.caption("Connection is checked when you send a message.")
        else:
            st.warning(health.get("configuration_error") or "Configure Azure OpenAI in .env to enable chat.")
    except httpx.HTTPError:
        st.warning("Start the API with python app.py")
    st.divider()
    st.caption("Refunds update the ledger only. This console does not send payments.")

st.title("💳 Billing Agent")
st.caption(
    "Ask questions about invoices, orders, refunds, "
    "billing, spending, and costs."
)


def show_invoices(entry, index):
    for n, invoice in enumerate(entry.get("invoices", [])):
        if st.button(f"Prepare PDF · {invoice['invoice_number']}", key=f"pdf-{index}-{n}"):
            try:
                pdf = request("GET", f"/invoices/{invoice['invoice_number']}", params={
                    "conversation_id": st.session_state.conversation_id, "customer_id": invoice["customer_id"]})
                st.download_button("Download invoice", pdf.content, "invoice.pdf", "application/pdf", key=f"dl-{index}-{n}")
            except httpx.HTTPError as exc:
                st.error(f"Invoice unavailable: {exc}")


for i, entry in enumerate(st.session_state.messages):
    with st.chat_message(entry["role"]):
        if entry["role"] == "assistant":
            st.text(plain_reply(entry["content"]))
        else:
            st.markdown(entry["content"])
        if st.session_state.mode == 'Admin' and entry.get('charts'):
            from ui.charts import render_chart
            for chart in entry.get('charts', []):
                render_chart(chart)
        show_invoices(entry, i)

prompt = st.chat_input("Ask a billing question...", disabled=bool(st.session_state.pending))
if prompt:
    try:
        if not st.session_state.conversation_id:
            st.session_state.conversation_id = request("POST", "/conversations", json={
                "customer_id": customer.strip() or None, 'mode': st.session_state.mode.lower()}).json()["conversation_id"]
        st.session_state.pending = {"message": prompt, "request_id": str(uuid4()), "conversation_id": st.session_state.conversation_id}
        st.session_state.messages.append({"role": "user", "content": prompt})
        st.rerun()
    except httpx.HTTPError as exc:
        st.error(f"Could not start the conversation: {exc}")

if st.session_state.pending:
    try:
        with st.chat_message("assistant"):
            with st.spinner("Billing Agent is thinking..."):
                reply = request("POST", "/chat", json=st.session_state.pending).json()
        st.session_state.messages.append({"role": "assistant", "content": reply["response"],
            "invoices": reply.get("invoices", []), 'charts':reply.get('charts',[])})
        st.session_state.pending = None
        st.rerun()
    except httpx.HTTPError as exc:
        detail = error_reply(exc.response) if isinstance(exc, httpx.HTTPStatusError) else "Unable to reach the Billing Agent. Check that the API is running."
        st.error(f"Request failed: {detail}")
        st.caption("Check current order state before repeating a write. A timed-out request may have completed.")
        if st.button("Retry same request"):
            st.rerun()
        if st.button("Clear pending request"):
            st.session_state.pending = None
            st.rerun()
