import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4
from config import settings


@contextmanager
def connection():
    conn = sqlite3.connect(settings.CONTEXT_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    settings.CONTEXT_DB.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            conversation_id TEXT PRIMARY KEY, customer_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
            request_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'done', response_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(conversation_id,request_id,role));
        CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id,id);
        CREATE TABLE IF NOT EXISTS shopping_states (
            conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
            state_json TEXT NOT NULL CHECK(json_valid(state_json)),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        """)
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(conversations)')}
        if 'mode' not in columns:
            # Older transcripts may contain store-wide data; require admin to resume them.
            conn.execute("ALTER TABLE conversations ADD COLUMN mode TEXT NOT NULL DEFAULT 'admin'")


def conversation(conversation_id=None, customer_id=None, mode='customer'):
    with connection() as conn:
        if conversation_id:
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if not row:
                raise ValueError("Conversation not found.")
            if customer_id != row["customer_id"]:
                raise ValueError("Customer association cannot change. Start a new conversation.")
            return conversation_id
        conversation_id = str(uuid4())
        conn.execute("INSERT INTO conversations(conversation_id,customer_id,mode) VALUES (?,?,?)", (conversation_id, customer_id, mode))
        return conversation_id


def associate_customer(conversation_id, customer_id):
    with connection() as conn:
        conn.execute("UPDATE conversations SET customer_id=? WHERE conversation_id=? AND customer_id IS NULL AND mode='customer'",
                     (customer_id, conversation_id))


def get_conversation(conversation_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        return dict(row) if row else None


def shopping_state(conversation_id):
    from context.shopping_state import empty_state
    with connection() as conn:
        row = conn.execute('SELECT state_json FROM shopping_states WHERE conversation_id=?',
                           (conversation_id,)).fetchone()
        defaults = empty_state()
        saved = json.loads(row[0]) if row else {}
        return {**defaults, **{key: value for key, value in saved.items() if key in defaults}}


def write_shopping_state(conn, conversation_id, state):
    conn.execute('''INSERT INTO shopping_states(conversation_id,state_json) VALUES (?,?)
        ON CONFLICT(conversation_id) DO UPDATE SET state_json=excluded.state_json,
        updated_at=CURRENT_TIMESTAMP''', (conversation_id, json.dumps(state)))


def checkpoint_shopping(conversation_id, request_id, state, customer_id):
    """Persist observed tool facts even if a later model call fails or times out."""
    with connection() as conn:
        record = conn.execute('''SELECT c.customer_id FROM conversations c JOIN messages m
            ON m.conversation_id=c.conversation_id WHERE c.conversation_id=? AND c.mode='customer'
            AND m.request_id=? AND m.role='user' AND m.status='pending' ''',
            (conversation_id, request_id)).fetchone()
        if record is None or (record['customer_id'] and record['customer_id'] != customer_id):
            raise ValueError('Shopping checkpoint does not match the active conversation.')
        if customer_id and not record['customer_id']:
            conn.execute('UPDATE conversations SET customer_id=? WHERE conversation_id=?',
                         (customer_id, conversation_id))
        write_shopping_state(conn, conversation_id, state)


def history(conversation_id, include_artifacts=False):
    with connection() as conn:
        rows = conn.execute("SELECT m.role,m.content,u.response_json FROM messages m LEFT JOIN messages u "
                            "ON m.role='assistant' AND u.role='user' AND u.conversation_id=m.conversation_id "
                            "AND u.request_id=m.request_id WHERE m.conversation_id=? "
                            "AND m.status='done' ORDER BY m.id DESC LIMIT 40", (conversation_id,)).fetchall()
        result = []
        for row in reversed(rows):
            message = {'role':row['role'],'content':row['content']}
            if include_artifacts and row['role']=='assistant' and row['response_json']:
                saved = json.loads(row['response_json'])
                message.update(charts=saved.get('charts',[]),invoices=saved.get('invoices',[]))
            result.append(message)
        return result


def begin(conversation_id, request_id, message):
    with connection() as conn:
        row = conn.execute("SELECT * FROM messages WHERE conversation_id=? AND request_id=? AND role='user'",
                           (conversation_id, request_id)).fetchone()
        if row:
            if row["content"] != message:
                raise ValueError("Request ID was already used with different content.")
            if row["response_json"]:
                return json.loads(row["response_json"])
            raise ValueError("This turn is pending or failed. Check order state before sending a new mutation.")
        conn.execute("INSERT INTO messages(conversation_id,request_id,role,content,status) VALUES (?,?,'user',?,'pending')",
                     (conversation_id, request_id, message))
        return None


def finish(conversation_id, request_id, response):
    with connection() as conn:
        if 'shopping_state' in response:
            record = conn.execute('SELECT mode FROM conversations WHERE conversation_id=?',
                                  (conversation_id,)).fetchone()
            if not record or record['mode'] != 'customer':
                raise ValueError('Only customer conversations have shopping state.')
            write_shopping_state(conn, conversation_id, response['shopping_state'])
        conn.execute("UPDATE messages SET status='done',response_json=? WHERE conversation_id=? AND request_id=? AND role='user'",
                     (json.dumps(response), conversation_id, request_id))
        conn.execute("INSERT INTO messages(conversation_id,request_id,role,content) VALUES (?,?,'assistant',?)",
                     (conversation_id, request_id, response["response"]))


def fail(conversation_id, request_id):
    with connection() as conn:
        conn.execute("UPDATE messages SET status='failed' WHERE conversation_id=? AND request_id=?", (conversation_id, request_id))


def discard_pending(conversation_id, request_id):
    """Only used for configuration rejection before any model/tool execution."""
    with connection() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id=? AND request_id=? "
                     "AND role='user' AND status='pending'", (conversation_id, request_id))










"""Plain customer-facing text; structured tool and memory payloads stay internal."""
import json
import re

REPLY_POLICY = """
VISIBLE WRITING STYLE AND OUTPUT BOUNDARY
All text the user sees must be natural plain English. Preserve exact item/customer/order/invoice
identifiers, amounts, currency codes, units and names. Use short paragraphs and practical explanations.
Write amounts with currency codes/names and discounts with the word percent.
Do not use raw JSON, SQL, tool arguments/results, code blocks, Markdown headings/tables/bullets,
emphasis, emoji, decorative symbols or internal-state fields INSIDE the visible answer.
Plain numbered product options are allowed when needed for references; keep their visible order stable.
Explain comparisons in sentences with the basis and unknown features. Clearly label page scope,
units, current prices versus billed prices, estimates and local ledger actions when relevant.
Avoid repeating the same answer, generic disclaimers, internal architecture and automatic upselling.
Do not claim a chart/PDF exists unless the tool returned a successful attachment result.

CUSTOMER MODE: the final transport output is EXACTLY ONE JSON object specified by SHOPPING POLICY,
with reply and shopping_update. Put all visible English inside reply exactly once. Write NOTHING
before or after the JSON object. Do not output plain text and then a second {"reply":...} object.
The no-JSON writing rule applies inside reply, not to the required internal transport envelope.
ADMIN MODE: return ONE plain-English answer with no reply/shopping_update JSON wrapper.
Internal tool-call arguments follow their own structured schemas in either mode.
Charts and invoice PDFs are separate UI attachments, not invented URLs in the text.
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







"""Small persistent shopping memory. Preferences are model interpreted; orders are DB facts."""
from copy import deepcopy
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from context.reply_text import plain_reply, reply_envelope
import logging
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from database.connection import connect

log = logging.getLogger(__name__)
ShortText = Annotated[str, Field(min_length=1, max_length=300)]
ProductID = Annotated[str, Field(pattern=r'^[A-Za-z0-9_-]{1,50}$')]


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')
    scope: Literal['unknown', 'per_item', 'merchandise_total', 'all_in'] = 'unknown'


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['place_order', 'replace_items', 'cancel_draft', 'create_customer',
                    'update_customer', 'process_refund']
    order_id: ProductID | None = None
    summary: str = Field(min_length=1, max_length=1500)


class ShoppingUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reset_preferences: bool = False
    needs: list[ShortText] = Field(default_factory=list, max_length=10)
    budget: Budget | None = None
    preferred_features: dict[str, str | None] = Field(default_factory=dict)
    quantity: int | None = Field(default=None, ge=1, le=100000, strict=True)
    displayed_product_ids: list[ProductID] = Field(default_factory=list, max_length=10)
    selected_product_ids: list[ProductID] = Field(default_factory=list, max_length=10)
    active_draft_id: ProductID | None = None
    pending_confirmation: Proposal | None = None
    next_question: str | None = Field(default=None, max_length=500)

    @field_validator('preferred_features')
    @classmethod
    def bounded_features(cls, values):
        if len(values) > 20 or any(not key.strip() or len(key) > 100 or
                (value is not None and len(value) > 300) for key, value in values.items()):
            raise ValueError('Use up to 20 short feature preferences.')
        return values


def empty_state():
    return {'schema_version': 1, 'stage': 'discover', 'needs': [], 'budget': None,
            'preferred_features': {}, 'quantity': None, 'known_products': [],
            'compared_product_ids': [], 'displayed_product_ids': [], 'selected_product_ids': [],
            'current_draft': None, 'last_order': None, 'pending_confirmation': None,
            'next_question': None, 'product_query': None}


def order_snapshot(order_id, customer_id):
    if not customer_id or not order_id:
        return None
    with closing(connect(readonly=True)) as conn:
        conn.execute('BEGIN')
        row = conn.execute('''SELECT o.*,d.status,d.currency FROM orders o
            LEFT JOIN order_details d ON d.order_id=o.order_id
            WHERE o.order_id=? AND o.customer_id=?''', (order_id, customer_id)).fetchone()
        if row is None:
            return None
        state = 'placed' if row['order_invoice'] else 'cancelled' if row['status'] == 'cancelled' else 'draft'
        currency = row['currency'] or conn.execute("SELECT value FROM billing_config WHERE key='currency'").fetchone()[0]
        result = {'order_id': row['order_id'], 'status': state, 'invoice_number': row['order_invoice'],
                  'currency': currency, 'promo_code': row['promo_code'],
                  'totals': {k:format(Decimal(str(row[k])), '.2f') for k in
                             ('subtotal','discount_applied','tax_amount','shipping_amount','charged_price')},
                  'items': [dict(r) for r in conn.execute('''SELECT r.item_code,i.name,r.quantity
                      FROM order_requests r JOIN items i ON i.item_code=r.item_code
                      WHERE r.order_id=? ORDER BY r.item_code''', (order_id,))]}
    result['fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return result


class ShoppingTracker:
    def __init__(self, state, customer_id, checkpoint=None):
        defaults = empty_state()
        self.state = {**defaults, **{key: deepcopy(value) for key, value in (state or {}).items()
                                    if key in defaults}}
        self.customer_id = customer_id
        self.checkpoint = checkpoint
        self.completed_actions = set()
        self.refresh()
        # The previous proposal is context for this user message. A new reply must
        # explicitly renew a proposal; an unrelated answer must not leave a stale 'yes'.

    def refresh(self):
        draft = self.state['current_draft']
        if draft:
            current = order_snapshot(draft['order_id'], self.customer_id)
            if not current or current['fingerprint'] != draft.get('fingerprint'):
                self.state['pending_confirmation'] = None
            self.set_order(current)
        proposal = self.state['pending_confirmation']
        if proposal and proposal.get('order_id'):
            current = order_snapshot(proposal['order_id'], self.customer_id)
            if not current or current['fingerprint'] != proposal.get('order_fingerprint'):
                self.state['pending_confirmation'] = None
        self.stage()

    def set_order(self, snapshot):
        if snapshot and snapshot['status'] == 'draft':
            self.state['current_draft'] = snapshot
        else:
            self.state['current_draft'] = None
            if snapshot:
                self.state['last_order'] = {k:snapshot[k] for k in ('order_id','status','invoice_number')}

    def stage(self):
        self.state['stage'] = ('confirmation' if self.state['pending_confirmation'] else
            'quote' if self.state['current_draft'] else 'after_sale' if self.state['last_order'] else
            'selection' if self.state['selected_product_ids'] else
            'comparison' if self.state['compared_product_ids'] else
            'browse' if self.state['needs'] or self.state['known_products'] else 'discover')

    def remember_products(self, ids):
        ids = list(dict.fromkeys(i for i in ids if isinstance(i,str)))[:200]
        if not ids:
            return
        with closing(connect(readonly=True)) as conn:
            rows = dict(conn.execute('SELECT item_code,name FROM items WHERE item_code IN (' +
                                    ','.join('?' for _ in ids)+')', ids))
        known = {p['item_code']:p for p in self.state['known_products']}
        for code in ids:
            if code in rows:
                known.pop(code, None)
                known[code] = {'item_code':code, 'name':rows[code][:200]}
        self.state['known_products'] = list(known.values())[-40:]

    def observe(self, name, arguments, result, customer_id):
        if not result.get('ok'):
            return
        self.customer_id = customer_id
        if name == 'compare_products':
            ids = [p['item_code'] for p in result.get('products', [])]
            self.remember_products(ids)
            self.state['compared_product_ids'] = ids
        elif name == 'find_cheaper_alternatives':
            self.remember_products([result['source']['item_code']] +
                                   [p['item_code'] for p in result.get('alternatives', [])])
        elif name == 'execute_sql_query' and 'item_code' in result.get('columns', []):
            self.state['product_query'] = {
                'query': arguments.get('query'),
                'parameters': deepcopy(arguments.get('parameters') or []),
                'row_count': result.get('row_count', 0),
                'truncated': result.get('truncated', False)}
            index = result['columns'].index('item_code')
            self.remember_products([row[index] for row in result.get('rows', [])])
        elif name in ('request_order','manage_draft','apply_promotion','place_order','process_refund'):
            snapshot = order_snapshot(result.get('order_id'), customer_id)
            if name != 'process_refund':
                self.set_order(snapshot)
            elif snapshot:
                self.state['last_order'] = {k:snapshot[k] for k in ('order_id','status','invoice_number')}
            if snapshot:
                self.remember_products([p['item_code'] for p in snapshot['items']])
            self.state['pending_confirmation'] = None
            action = arguments.get('action') if name == 'manage_draft' else name
            self.completed_actions.add(('cancel_draft' if action == 'cancel' else action, result.get('order_id')))
        elif name == 'manage_customer':
            self.state['pending_confirmation'] = None
            self.completed_actions.add((arguments.get('action','create')+'_customer', None))
        self.stage()
        if self.checkpoint:
            self.checkpoint(deepcopy(self.state), self.customer_id)

    def apply_update(self, values, reply):
        parsed = ShoppingUpdate.model_validate(values)
        update = parsed.model_dump(mode='json', exclude_unset=True)
        if parsed.budget is not None:
            update['budget'] = parsed.budget.model_dump(mode='json')
        # Validate all product references before applying any preference changes.
        allowed = {p['item_code'] for p in self.state['known_products']}
        for key in ('selected_product_ids','displayed_product_ids','compared_product_ids'):
            allowed.update(self.state[key])
        for key in ('displayed_product_ids','selected_product_ids'):
            if key in update and (len(set(update[key])) != len(update[key]) or not set(update[key]) <= allowed):
                raise ValueError('Product selections must come from observed product results.')
        snapshot = None
        if update.get('active_draft_id'):
            snapshot = order_snapshot(update['active_draft_id'], self.customer_id)
            if not snapshot or snapshot['status'] != 'draft':
                raise ValueError('Active draft must belong to the associated customer.')
        features = {} if update.get('reset_preferences') else dict(self.state['preferred_features'])
        for key, value in update.get('preferred_features', {}).items():
            if value is None:
                features.pop(key, None)
            else:
                features[key] = value
        if len(features) > 20:
            raise ValueError('Consolidate preferences before adding more than 20 features.')
        if update.get('reset_preferences'):
            for key in ('needs','budget','preferred_features','quantity','compared_product_ids',
                        'displayed_product_ids','selected_product_ids'):
                self.state[key] = empty_state()[key]
            self.state['last_order'] = None
            self.state['product_query'] = None
        for key in ('needs','budget','quantity','displayed_product_ids','selected_product_ids'):
            if key in update:
                self.state[key] = update[key]
        self.state['preferred_features'] = features
        if 'active_draft_id' in update:
            self.set_order(snapshot)
        self.state['next_question'] = update.get('next_question')
        proposed = update.get('pending_confirmation')
        if proposed and proposed['summary'] in reply and (proposed['action'], proposed.get('order_id')) not in self.completed_actions:
            order_id = proposed.get('order_id')
            order = order_snapshot(order_id, self.customer_id) if order_id else None
            action = proposed['action']
            is_customer = action in ('create_customer','update_customer')
            expected = 'placed' if action == 'process_refund' else 'draft'
            if (is_customer and not order_id) or (not is_customer and order and order['status'] == expected):
                proposed['status'] = 'awaiting_customer'
                proposed['order_fingerprint'] = order['fingerprint'] if order else None
                self.state['pending_confirmation'] = proposed
        self.stage()

    def finish(self, content):
        self.state['pending_confirmation'] = None
        self.state['next_question'] = None
        raw = content.strip()
        if raw.startswith('```json') and raw.endswith('```'):
            raw = raw[7:-3].strip()
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            envelope = reply_envelope(raw)
            if envelope is None:
                if '"shopping_update"' in raw and (raw.startswith('{') or raw.startswith('```')):
                    raise ValueError('Shopping response JSON is incomplete.') from None
                # Plain text responses remain usable. Never guess preferences from keywords.
                self.stage()
                return plain_reply(content)
        if not isinstance(envelope,dict) or 'shopping_update' not in envelope:
            self.stage()
            return plain_reply(content)
        reply = envelope.get('reply')
        if not isinstance(reply,str) or not reply.strip():
            raise ValueError('Customer reply is missing from the shopping response.')
        reply = plain_reply(reply)
        update = deepcopy(envelope['shopping_update'])
        if isinstance(update, dict):
            proposal = update.get('pending_confirmation')
            if isinstance(proposal, dict) and isinstance(proposal.get('summary'), str):
                proposal['summary'] = plain_reply(proposal['summary'])
            if isinstance(update.get('next_question'), str):
                update['next_question'] = plain_reply(update['next_question'])
        saved = deepcopy(self.state)
        try:
            self.apply_update(update, reply)
        except (ValidationError, ValueError, TypeError):
            self.state = saved
            self.stage()
            log.warning('Invalid shopping update ignored; persisted facts and preferences retained.')
            reply += '\n\nI could not save the updated shopping preferences for this reply. Please repeat any changed preferences next time.'
        return reply
