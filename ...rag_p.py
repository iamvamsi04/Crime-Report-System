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
        return json.loads(row[0]) if row else empty_state()


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
