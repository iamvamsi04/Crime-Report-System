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
