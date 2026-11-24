"""Render saved, query-backed charts with the existing Altair/Streamlit stack."""
import logging
import re
from datetime import datetime

import altair as alt
import streamlit as st

PALETTE = ['#2563eb', '#008578', '#c57812', '#8654bd', '#cc4e65', '#387fa1',
           '#638331', '#bc5a2b', '#6155a6', '#a75083']
MAX_PIE_SLICES = 12


def build_chart(chart):
    from mcp_server.charts import validate_saved_chart
    return _build_validated_chart(validate_saved_chart(chart))


def _build_validated_chart(chart):
    kind, x, ys = chart['type'], chart['x'], chart['y']
    axis = chart.get('x_type', 'numeric' if kind in ('scatter','bubble','histogram') else 'temporal' if kind=='timeline' else 'categorical')
    xtype = {'categorical':'O','numeric':'Q','temporal':'T'}[axis]
    currency = chart.get('currency')
    suffix = f' ({currency})' if currency else ''
    def label_title(field):
        return field.replace('_', ' ').capitalize()

    def field_title(field):
        counts = re.search(r'(?:^|[^a-z0-9])(?:counts?|qty|quantity|quantities|units|products|orders|invoices|customers|items|percent|percentage|ratio|share|rate)(?:[^a-z0-9]|$)|quantity$', field, re.I)
        return label_title(field) + (suffix if not counts else '')

    def value_format(field):
        return ',.2f' if currency and field_title(field).endswith(suffix) else ',.6~g'

    measure = field_title(ys[0]) if len(ys)==1 else 'Products' if all(
        field.endswith('products') for field in ys) else 'Value' + (
        suffix if any(field_title(field).endswith(suffix) for field in ys) and suffix else '')
    rows = []
    for i, record in enumerate(chart['data']):
        row = {'x_value':record[x], 'position':i}
        if axis == 'temporal' and datetime.fromisoformat(row['x_value']).tzinfo is None:
            row['x_value'] += 'Z'
        row.update({f'v{j}':record[y] for j,y in enumerate(ys)})
        for source,target in [('group','category'),('size','bubble_size'),('end','end_value'),('label','point_label')]:
            if source in chart:
                row[target] = record[chart[source]]
                if source == 'end' and row[target] is not None and datetime.fromisoformat(row[target]).tzinfo is None:
                    row[target] += 'Z'
        rows.append(row)

    def base(data=None):
        return alt.Chart(alt.Data(values=rows if data is None else data))

    coordinates = {row['x_value'] for row in rows}
    time_bar_width = max(2., min(42., 460. / len(coordinates)))

    def color(field='series:N', title='Measure', cardinality=None):
        scale = alt.Scale(scheme='tableau20') if cardinality and cardinality > len(PALETTE) else alt.Scale(range=PALETTE)
        return alt.Color(field, title=label_title(title), scale=scale,
                         legend=alt.Legend(orient='bottom', columns=3, labelLimit=135, rowPadding=6))

    def finish(result, height=340):
        return (result.properties(height=height, description=chart['title'])
                .configure_view(strokeWidth=0)
                .configure_axis(domain=False, tickSize=0, labelPadding=7, labelFontSize=12,
                                titleFontSize=12, titlePadding=12, gridColor='#94a3b8',
                                gridOpacity=0.25, gridDash=[2, 3], labelLimit=180)
                .configure_legend(labelFontSize=12, titleFontSize=12, symbolSize=96))

    order = alt.SortField(field='position', order='ascending')
    axis_options = {}
    time_format = '%Y-%m-%d %H:%M:%S.%L'
    if xtype == 'O':
        dense = len(coordinates) > 8 or max(map(len, coordinates)) > 18
        axis_options = {'sort': order, 'axis': alt.Axis(
            labelAngle=-30 if dense and kind != 'horizontal_bar' else 0, labelOverlap='greedy',
            labelLimit=240 if kind == 'horizontal_bar' else 160)}
    elif xtype == 'T' and kind != 'timeline':
        dates = [datetime.fromisoformat(row['x_value'].replace('Z', '+00:00')) for row in rows]
        months = {day.year*12+day.month for day in dates}
        if all(day.day == 1 and not (day.hour or day.minute or day.second or day.microsecond) for day in dates):
            span = max(months)-min(months)
            yearly = len({day.month for day in dates}) == 1 and span >= 12
            time_format = '%Y' if yearly else '%b %Y'
            axis_options = {'axis': alt.Axis(format=time_format,
                tickCount={'interval':'year' if yearly else 'month', 'step':max(1, span//120 if yearly else (span+11)//12)},
                labelOverlap='greedy')}
        elif all(not (day.hour or day.minute or day.second or day.microsecond) for day in dates):
            time_format = '%d %b %Y'
            axis_options = {'axis':alt.Axis(format=time_format,labelOverlap='greedy')}
        else:
            axis_options = {'axis':alt.Axis(format=time_format,labelOverlap='greedy')}
    if xtype == 'T':
        axis_options['scale'] = alt.Scale(type='utc')
    xc = alt.X('x_value:'+xtype, title=field_title(x) if xtype=='Q' else label_title(x),
               **axis_options)
    multiple_series = len(ys)>1 or 'group' in chart
    discrete_bars = kind in ('bar','grouped_bar','horizontal_bar') and multiple_series and xtype != 'O'
    temporal_heatmap = kind == 'heatmap' and xtype == 'T'
    # Continuous-axis offsets overlap grouped bars, and heatmap periods need
    # distinct, equal-width cells. Format labels without changing the positions.
    if discrete_bars or temporal_heatmap:
        discrete_axis = alt.Axis(labelAngle=-30 if len(coordinates)>8 and kind!='horizontal_bar' else 0, labelOverlap='greedy',
            **({'labelExpr':f"utcFormat(toDate(datum.value), '{time_format}')"} if xtype=='T' else {}))
        discrete_order = sorted(coordinates, key=datetime.fromisoformat) if temporal_heatmap else order
        xc = alt.X('x_value:O',title=label_title(x),sort=discrete_order,axis=discrete_axis)
    tip = [alt.Tooltip('point_label:N', title=label_title(chart['label']))] if 'label' in chart else []
    tip.append(alt.Tooltip('x_value:'+('N' if temporal_heatmap else xtype),
                           title=field_title(x) if xtype=='Q' else label_title(x),
                           **({'format':value_format(x)} if xtype=='Q' else {})))
    tip += [alt.Tooltip(f'v{i}:Q', title=field_title(y), format=value_format(y)) for i,y in enumerate(ys)]
    if 'group' in chart:
        tip.append(alt.Tooltip('category:N',title=label_title(chart['group'])))

    if kind == 'pie':
        total = sum(row['v0'] for row in rows)
        positive = [row for row in rows if row['v0'] > 0]
        if len(positive) > MAX_PIE_SLICES:
            ranked = sorted(positive, key=lambda row: row['v0'], reverse=True)
            other = 'Other'
            number = 2
            while other in coordinates:
                other = f'Other ({number})'
                number += 1
            keep = MAX_PIE_SLICES - 1
            positive = ranked[:keep] + [{'x_value': other, 'v0': sum(row['v0'] for row in ranked[keep:])}]
        segments = [{**row, 'position': index, 'share': row['v0'] / total}
                    for index, row in enumerate(positive)]
        spacing = 0.013 if min(row['share'] for row in segments) >= 0.015 else 0
        c = base(segments).mark_arc(outerRadius=145, padAngle=spacing).encode(
            theta=alt.Theta('v0:Q'), color=color('x_value:N',x,len(segments)), order='position:O',
            tooltip=tip+[alt.Tooltip('share:Q',title='Share of query total',format='.1%')])
        return finish(c)
    if kind in ('scatter','bubble'):
        scale = alt.Scale(zero=False, padding=18)
        c = base().mark_circle(opacity=0.55 if len(rows)>80 else 0.78,
                               stroke='white',strokeWidth=0.65,
                               **({'size': 45 if len(rows)>80 else 80} if kind=='scatter' else {})).encode(
            x=alt.X('x_value:Q',title=field_title(x),scale=scale),
            y=alt.Y('v0:Q',title=measure,scale=scale),tooltip=tip)
        if 'group' in chart:
            c = c.encode(color=color('category:N',chart['group'],len({row['category'] for row in rows})))
        else:
            c = c.encode(color=alt.value(PALETTE[0]))
        if kind=='bubble':
            # Vega expresses circle size as area. Preserve proportional area and
            # draw smaller bubbles above larger ones where points overlap.
            max_area = max(300, min(1500, round(6400 / len(rows)**0.5)))
            size = chart['size']
            c = c.encode(size=alt.Size('bubble_size:Q',title=field_title(size),
                                      scale=alt.Scale(type='linear',zero=True,range=[0,max_area]),
                                      legend=alt.Legend(orient='bottom',tickCount=4)),
                         order=alt.Order('bubble_size:Q',sort='descending'),
                         tooltip=tip+[alt.Tooltip('bubble_size:Q',title=field_title(size),format=value_format(size))])
        return finish(c, 380)
    if kind=='histogram':
        return finish(base().mark_bar(color=PALETTE[0],binSpacing=2).encode(
            x=alt.X('x_value:Q',bin=alt.Bin(maxbins=chart.get('bins',10)),title=field_title(x)),
            y=alt.Y('count():Q',title='Observations'),
            tooltip=[alt.Tooltip('x_value:Q',bin=True,title=x),alt.Tooltip('count():Q',title='Observations')]))
    if kind=='boxplot':
        c = base().mark_boxplot(extent=1.5,size=28).encode(x=xc,y=alt.Y('v0:Q',title=measure))
        if 'group' in chart:
            c = c.encode(color=color('category:N',chart['group']),xOffset='category:N')
        else:
            c = c.encode(color=alt.value(PALETTE[0]))
        return finish(c)
    if kind=='heatmap':
        values = [row['v0'] for row in rows]
        heat = alt.Scale(scheme='redblue', domainMid=0) if min(values)<0<max(values) else alt.Scale(scheme='blues')
        categories = {row['category'] for row in rows}
        crowded = len(coordinates) > 30 or len(categories) > 24
        cells = base().mark_rect(
            **({} if crowded else {'cornerRadius': 2, 'stroke': 'white', 'strokeWidth': 0.65}))
        return finish(cells.encode(
            x=xc, y=alt.Y('category:N', title=label_title(chart['group'])),
            color=alt.Color('v0:Q', title=measure, scale=heat,
                            legend=alt.Legend(orient='bottom', gradientLength=160,
                                              gradientThickness=9, tickCount=4)), tooltip=tip),
            max(260, min(720, len(categories) * 30)))
    if kind=='range':
        c = base().mark_rule(strokeWidth=7,color=PALETTE[0]).encode(
            x=xc,y=alt.Y('v0:Q',title='Range'+suffix,scale=alt.Scale(zero=False)),y2='v1:Q',tooltip=tip)
        endpoints = base().mark_tick(color=PALETTE[0],size=18,thickness=2).encode(x=xc,y='v0:Q')
        return finish(c + endpoints + endpoints.encode(y='v1:Q'))
    if kind=='combo':
        bars = base().mark_bar(color=PALETTE[0],opacity=0.7,
                              **({'size':time_bar_width} if xtype=='T' else {})).encode(
            x=xc,y=alt.Y('v0:Q',title=field_title(ys[0]),
                        axis=alt.Axis(titleColor=PALETTE[0],labelColor=PALETTE[0])),tooltip=tip)
        line = base().mark_line(color=PALETTE[1],point=True,strokeWidth=3).encode(
            x=xc,y=alt.Y('v1:Q',title=field_title(ys[1]),axis=alt.Axis(orient='right',grid=False,
                titleColor=PALETTE[1],labelColor=PALETTE[1])),tooltip=tip)
        return finish(alt.layer(bars,line).resolve_scale(y='independent'))
    if kind=='timeline':
        if 'end' in chart:
            tip.append(alt.Tooltip('end_value:T',title=label_title(chart['end'])))
        date_axis = alt.X('x_value:T',title='Recorded dates' if 'end' in chart else label_title(x),
                          scale=alt.Scale(type='utc'))
        task_axis = alt.Y('category:N',title=label_title(chart['group']),sort=order)
        starts = base().mark_circle(size=65,color=PALETTE[0]).encode(
            x=date_axis,y=task_axis,tooltip=tip)
        closed = [row for row in rows if row.get('end_value') is not None]
        if closed:
            intervals = base(closed).mark_bar(color=PALETTE[0],cornerRadius=4,size=14,opacity=0.65).encode(
                x=date_axis,x2='end_value:T',y=task_axis,tooltip=tip)
            starts = intervals + starts
        return finish(starts,max(300,min(900,len({r['category'] for r in rows})*30)))
    series_labels = [y.replace('_',' ').capitalize() for y in ys]
    if len(set(series_labels)) != len(series_labels):
        series_labels = list(ys)
    long = [{'x_value':r['x_value'],'value':r[f'v{j}'],'series':r.get('category',series_labels[j]),'position':r['position']}
            for r in rows for j,y in enumerate(ys)]
    b = base(long)
    if kind in ('bar','grouped_bar','horizontal_bar','stacked_bar','normalized_bar'):
        groups = len(ys) if len(ys)>1 else len({row.get('category') for row in rows}) if 'group' in chart else 1
        c = b.mark_bar(cornerRadiusEnd=3, **({'size':max(2.,time_bar_width/max(1,groups))}
            if xtype=='T' else {}))
    elif kind == 'line':
        c = b.mark_line(point=True,strokeWidth=2.5)
    else:
        raise ValueError('Unsupported chart type.')
    stack = 'normalize' if kind.startswith('normalized') else 'zero' if kind.startswith('stacked') else None
    title = 'Share' if stack=='normalize' else measure
    series_title = chart.get('group','Measure')
    series = {row['series'] for row in long}
    series_tip = [tip[0]]
    if multiple_series:
        series_tip.append(alt.Tooltip('series:N',title=label_title(series_title)))
    series_tip.append(alt.Tooltip('value:Q',title=measure,format=value_format(ys[0])))
    c = c.encode(color=color('series:N',series_title,len(series)),tooltip=series_tip)
    if len(ys) == 1 and 'group' not in chart:
        c = c.encode(color=alt.value(PALETTE[0]))
    value_axis = alt.Axis(format='%') if stack=='normalize' else alt.Axis()
    if kind=='horizontal_bar':
        c = c.encode(y=alt.Y('x_value:'+('O' if discrete_bars else xtype),title=x.replace('_',' ').capitalize(),sort=order,
                            **({'axis':discrete_axis} if discrete_bars else
                               {key:value for key,value in axis_options.items() if key!='sort'})),
                     x=alt.X('value:Q',title=title,stack=None,axis=value_axis))
        if len(ys)>1 or 'group' in chart:
            c = c.encode(yOffset='series:N')
        return finish(c,max(320,min(900,len({r['x_value'] for r in rows})*30)))
    c = c.encode(x=xc,y=alt.Y('value:Q',title=title,stack=stack,axis=value_axis))
    if kind in ('bar','grouped_bar') and (len(ys)>1 or 'group' in chart):
        c = c.encode(xOffset='series:N')
    return finish(c)


def render_chart(chart):
    try:
        from mcp_server.charts import validate_saved_chart
        chart = validate_saved_chart(chart)
        result=_build_validated_chart(chart)
        st.subheader(chart['title'])
        st.altair_chart(result,use_container_width=True)
        count = len(chart['data'])
        caption = f'{count:,} query row' + ('' if count == 1 else 's')
        if chart.get('currency'):
            caption += f" · {chart['currency']}"
        if chart['type'] == 'pie' and sum(row[chart['y'][0]] > 0 for row in chart['data']) > MAX_PIE_SLICES:
            caption += '. Smaller slices are grouped. View chart data for every category.'
        if chart['type'] == 'timeline' and 'end' in chart and any(row[chart['end']] is None for row in chart['data']):
            caption += '. Events without a recorded end are shown as points.'
        st.caption(caption)
        with st.expander('View chart data'):
            st.dataframe(chart['data'],hide_index=True,use_container_width=True)
    except Exception:
        logging.getLogger(__name__).exception('Unable to render saved chart')
        st.warning('This saved chart cannot be displayed. Ask the agent to generate it again with a supported type and valid data.')
        return False
    return True
