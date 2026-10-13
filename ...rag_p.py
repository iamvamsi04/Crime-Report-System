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
