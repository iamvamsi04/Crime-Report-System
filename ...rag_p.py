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
        """)


def conversation(conversation_id=None, customer_id=None):
    with connection() as conn:
        if conversation_id:
            row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if not row:
                raise ValueError("Conversation not found.")
            if customer_id != row["customer_id"]:
                raise ValueError("Customer association cannot change. Start a new conversation.")
            return conversation_id
        conversation_id = str(uuid4())
        conn.execute("INSERT INTO conversations(conversation_id,customer_id) VALUES (?,?)", (conversation_id, customer_id))
        return conversation_id


def get_conversation(conversation_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        return dict(row) if row else None


def history(conversation_id):
    with connection() as conn:
        rows = conn.execute("SELECT role,content FROM messages WHERE conversation_id=? "
                            "AND status='done' ORDER BY id DESC LIMIT 40", (conversation_id,)).fetchall()
        return [dict(row) for row in reversed(rows)]


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
