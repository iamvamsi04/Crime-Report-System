"""UTC price windows. Stored timestamps use a space and six fractional digits.

Naive input timestamps are UTC. Legacy date-only ends include the entire day.
"""
from datetime import date, datetime, time, timedelta, timezone
import re


def as_datetime(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        value = value.strip()
        if any(len(match) > 6 for match in re.findall(r'[.,](\d+)', value)):
            raise ValueError('Price timestamps support at most six fractional digits.')
        try:
            result = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError('Use a valid ISO date or timestamp for pricing.') from exc
    else:
        raise ValueError('Use a valid ISO date or timestamp for pricing.')
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    try:
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError('Price timestamp is outside the supported UTC range.') from exc


def normalize_timestamp(value):
    return as_datetime(value).replace(tzinfo=None).isoformat(sep=' ', timespec='microseconds')


def utc_now():
    return normalize_timestamp(datetime.now(timezone.utc))


def price_window(effective_from, effective_to):
    start = as_datetime(effective_from)
    end = None
    if effective_to is not None:
        end = as_datetime(effective_to)
        date_only = (isinstance(effective_to, date) and not isinstance(effective_to, datetime)
                     or isinstance(effective_to, str) and len(effective_to.strip()) == 10)
        if date_only:
            try:
                end += timedelta(days=1)
            except OverflowError as exc:
                raise ValueError('Price window end is outside the supported UTC range.') from exc
        if end <= start:
            raise ValueError('Price window end must be later than its start.')
    return start, end


def matching_prices(conn, item_code, as_of):
    instant = as_datetime(as_of)
    matches = []
    for row in conn.execute('SELECT * FROM price_history WHERE item_code=? ORDER BY effective_from,id', (item_code,)):
        start, end = price_window(row['effective_from'], row['effective_to'])
        if start <= instant and (end is None or instant < end):
            matches.append(row)
    return matches


def order_price_time(conn, order):
    fields = dict(order)
    instant = fields.get('price_as_of')
    if instant is None:
        row = conn.execute('SELECT price_as_of FROM order_details WHERE order_id=?',
                           (fields['order_id'],)).fetchone()
        instant = row[0] if row else None
    return normalize_timestamp(instant if instant is not None else fields['order_date'])


# SQLite dates have millisecond precision. Canonical UTC values retain an exact
# microsecond tie-break; Python validates mixed ISO forms before price lookup.
PRICE_WINDOW_CHECK = """julianday(effective_from) IS NOT NULL AND (
    effective_to IS NULL OR (julianday(effective_to) IS NOT NULL AND (
        julianday(effective_to) + CASE WHEN length(effective_to)=10 THEN 1 ELSE 0 END > julianday(effective_from)
        OR (julianday(effective_to)=julianday(effective_from) AND effective_to<>effective_from AND (
            NOT (length(effective_from)=26 AND substr(effective_from,11,1)=' ' AND substr(effective_from,20,1)='.'
                 AND substr(effective_from,21) NOT GLOB '*[^0-9]*'
                 AND length(effective_to)=26 AND substr(effective_to,11,1)=' ' AND substr(effective_to,20,1)='.'
                 AND substr(effective_to,21) NOT GLOB '*[^0-9]*')
            OR effective_to>effective_from)))))"""
