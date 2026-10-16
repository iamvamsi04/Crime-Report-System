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
