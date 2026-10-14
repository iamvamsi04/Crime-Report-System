"""Plain customer-facing text; structured tool and memory payloads stay internal."""
import json
import re

REPLY_POLICY = """
Write the visible reply in plain English, using short sentences and paragraphs.
Do not use JSON, code blocks, Markdown headings, tables, bullets, emphasis, emoji or
decorative symbols in the visible reply. Preserve exact product and invoice identifiers,
amounts, units and customer names. Use currency names or codes and the word percent.
Explain comparisons in sentences. Keep numbered options understandable in plain text.
Internal tool arguments and the customer shopping-memory envelope still use their required
structured format. Only the reply field is customer-facing. Charts and invoices are attachments.
"""

FALLBACK = 'I could not display that answer clearly. Please ask me to check the current records before repeating an order or refund request.'


def reply_envelope(text):
    """Find a complete internal envelope even when the model adds surrounding prose.

    Decode whole JSON objects so escaped quotes/braces and nested memory are safe.
    Never concatenate the prose and reply: the envelope is the canonical answer.
    """
    decoder = json.JSONDecoder()
    offset = 0
    envelope = None
    while True:
        start = text.find('{', offset)
        if start < 0:
            return envelope
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            offset = start + 1
            continue
        offset = end
        if isinstance(value, dict) and (
                isinstance(value.get('reply'), str) or isinstance(value.get('response'), str)):
            envelope = value


def plain_reply(content):
    text = str(content).strip()
    envelope = reply_envelope(text)
    if envelope is not None:
        visible = envelope.get('reply', envelope.get('response'))
        return plain_reply(visible) if isinstance(visible, str) and visible.strip() and visible != text else FALLBACK
    # A truncated appended envelope must not leak internal fields onto the screen.
    fragment = re.search(r'\{\s*"(?:reply|response|shopping_update)"\s*:', text)
    if fragment:
        text = re.sub(r'```(?:json)?\s*$', '', text[:fragment.start()]).strip()
        if not text:
            return FALLBACK
    text = re.sub(r'^```(?:json)?\s*\n?(.*?)\n?```$', r'\1', text, flags=re.S)
    if text.startswith(('{', '[')):
        try:
            payload = json.loads(text)
        except ValueError:
            if text.startswith('{') or re.match(r'^\[\s*[\{"\[]', text):
                return FALLBACK
        else:
            if not isinstance(payload, dict):
                return FALLBACK
            reply = payload.get('reply', payload.get('response'))
            if not isinstance(reply, str) or not reply.strip() or reply == text:
                return FALLBACK
            return plain_reply(reply)
    text = re.sub(r'!\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'</?(?:p|br|div|span|b|strong|i|em|code|pre|ul|ol|li|h[1-6])(?:\s+[^>]*)?\s*/?>', '', text)
    text = re.sub(r'(?m)^\s*(?:#{1,6}\s+|[-*+]\s+|>\s*)', '', text)
    text = re.sub(r'(\*\*|__|~~)(.+?)\1', r'\2', text, flags=re.S)
    text = re.sub(r'(?<!\w)([*_])([^\n]+?)\1(?!\w)', r'\2', text)
    text = text.replace('`', '')
    # Convert Markdown tables to labelled prose without losing column associations.
    lines, table = [], []
    def flush_table():
        if not table:
            return
        headers, *rows = table
        if not rows:
            lines.append('; '.join(headers))
        for cells in rows:
            lines.append('; '.join(f'{headers[i] if i < len(headers) else "Value"}: {c}' for i, c in enumerate(cells)) + '.')
        table.clear()
    for line in text.splitlines():
        if '|' in line:
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            if not all(re.fullmatch(r':?-+:?', c) for c in cells):
                table.append(cells)
        else:
            flush_table()
            lines.append(line)
    flush_table()
    text = '\n'.join(lines)
    for symbol, word in {'$':' dollars ', '₹':' INR ', '€':' EUR ', '£':' GBP ', '%':' percent',
                         '→':' to ', '≥':' at least ', '≤':' at most ', '×':' times ',
                         '°':' degrees ', '–':'-', '—':'. '}.items():
        text = text.replace(symbol, word)
    text = ''.join(c for c in text if not (0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF) and c not in '\u200d\ufe0f\u20e3')
    text = re.sub(r'(?m)^[ \t]*[-*_]{3,}[ \t]*$', '', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = '\n'.join(line.strip() for line in text.splitlines())
    return re.sub(r'\n{3,}', '\n\n', text).strip() or FALLBACK


def error_reply(response):
    """Extract the safe message, never display a raw HTTP JSON body."""
    try:
        detail = response.json().get('detail')
        if isinstance(detail, dict):
            detail = detail.get('message')
        if isinstance(detail, str):
            return plain_reply(detail)
    except (ValueError, AttributeError):
        pass
    return 'The request could not be completed. Check your connection and request details, then try again.'
