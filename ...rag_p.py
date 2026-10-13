"""Small persistent shopping memory. Preferences are model interpreted; orders are DB facts."""
from copy import deepcopy
from contextlib import closing
from decimal import Decimal
import hashlib
import json
from context.reply_text import plain_reply
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
            'next_question': None}


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
        self.state = {**empty_state(), **deepcopy(state or {})}
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
                raise ValueError('Product selections must come from observed catalogue results.')
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
