from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.models.Collection import Collection

from app.config import Settings
from app.errors import NotFoundError, StorageError
from app.models import ConversationContext, Evidence

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    file_type TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER,
    csv_profile TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT,
    sources_json TEXT,
    execution_flow_json TEXT,
    query_plan_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);
"""


DOCUMENT_COLUMNS = frozenset(
    {
        "id",
        "filename",
        "stored_name",
        "file_type",
        "file_hash",
        "status",
        "chunk_count",
        "page_count",
        "csv_profile",
        "error_message",
        "created_at",
        "updated_at",
    }
)
MESSAGE_COLUMNS = frozenset(
    {
        "id",
        "conversation_id",
        "role",
        "content",
        "status",
        "sources_json",
        "execution_flow_json",
        "query_plan_json",
        "created_at",
    }
)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Storage:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: sqlite3.Connection | None = None
        self._chroma: chromadb.PersistentClient | None = None
        self.collection: Collection | None = None
        self._lock = threading.Lock()

    def init(self) -> None:
        try:
            self.settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings.chroma_path.mkdir(parents=True, exist_ok=True)
            self.settings.upload_dir.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.settings.sqlite_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._chroma = chromadb.PersistentClient(path=str(self.settings.chroma_path))
            self.collection = self._chroma.get_or_create_collection(
                name="doc_chunks",
                metadata={"hnsw:space": "cosine"},
            )
            log.info("storage_initialized sqlite=%s chroma=%s", self.settings.sqlite_path, self.settings.chroma_path)
        except Exception as exc:
            log.exception("storage_init_failed")
            raise StorageError("Document storage could not be initialized.") from exc

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("Storage is not initialized.")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def insert_document(self, record: dict[str, Any]) -> None:
        cols = _allowed_columns(record, DOCUMENT_COLUMNS)
        placeholders = ", ".join("?" for _ in cols)
        with self._lock:
            self.conn.execute(
                f"INSERT INTO documents ({', '.join(cols)}) VALUES ({placeholders})",
                [record[c] for c in cols],
            )
            self.conn.commit()

    def update_document(self, document_id: str, **fields: Any) -> None:
        fields["updated_at"] = utcnow()
        cols = _allowed_columns(fields, DOCUMENT_COLUMNS)
        assignments = ", ".join(f"{k} = ?" for k in cols)
        with self._lock:
            self.conn.execute(
                f"UPDATE documents SET {assignments} WHERE id = ?",
                [*[fields[c] for c in cols], document_id],
            )
            self.conn.commit()

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if row is None:
            raise NotFoundError("Document was not found.")
        return dict(row)

    def get_document_by_hash(self, file_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM documents WHERE file_hash = ?", (file_hash,)).fetchone()
        return dict(row) if row else None

    def list_documents(self, ready_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM documents"
        if ready_only:
            sql += " WHERE status = 'ready'"
        sql += " ORDER BY created_at DESC"
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def delete_document(self, document_id: str) -> dict[str, Any]:
        doc = self.get_document(document_id)
        try:
            if self.collection is not None:
                self.collection.delete(where={"document_id": document_id})
        except Exception as exc:
            log.exception("chroma_delete_failed document_id=%s", document_id)
            raise StorageError("Failed to remove document embeddings.") from exc
        stored = self.upload_path(doc["stored_name"])
        if stored.exists():
            stored.unlink()
        with self._lock:
            self.conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self.conn.commit()
        log.info("document_deleted document_id=%s", document_id)
        return doc

    def purge_ingest(self, document_id: str) -> None:
        try:
            if self.collection is not None:
                self.collection.delete(where={"document_id": document_id})
        except Exception:
            log.exception("ingest_cleanup_chroma_failed document_id=%s", document_id)
        with self._lock:
            self.conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            self.conn.commit()

    def upload_path(self, stored_name: str) -> Path:
        base = self.settings.upload_dir.resolve()
        path = (base / Path(stored_name).name).resolve()
        if path != base and base not in path.parents:
            raise StorageError("Invalid stored file path.")
        return path

    def document_file(self, document_id: str) -> Path:
        doc = self.get_document(document_id)
        return self.upload_path(doc["stored_name"])

    def upsert_chunks(
        self,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        if self.collection is None:
            raise StorageError("Vector store is not initialized.")
        try:
            self.collection.upsert(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
            )
            log.info("chromadb_storage chunks=%s", len(ids))
        except Exception as exc:
            log.exception("chromadb_upsert_failed")
            raise StorageError("Failed to store document embeddings.") from exc

    def query_chunks(
        self,
        query_embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> list[Evidence]:
        if self.collection is None:
            raise StorageError("Vector store is not initialized.")
        try:
            kwargs: dict[str, Any] = {
                "query_embeddings": [query_embedding],
                "n_results": max(n_results, 1),
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where
            result = self.collection.query(**kwargs)
        except Exception as exc:
            log.exception("chromadb_query_failed")
            raise StorageError("Failed to search document embeddings.") from exc

        evidence: list[Evidence] = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for chunk_id, text, meta, distance in zip(ids, docs, metas, distances, strict=False):
            meta = meta or {}
            similarity = 1.0 - float(distance)
            evidence.append(
                Evidence(
                    document_id=str(meta.get("document_id", "")),
                    filename=str(meta.get("filename", "")),
                    document_type=str(meta.get("document_type", "")),
                    chunk_id=str(meta.get("chunk_id", chunk_id.split(":")[-1])),
                    text=text or "",
                    similarity=similarity,
                    page_number=int(meta.get("page_number") or 0),
                    section=str(meta.get("section") or ""),
                    source_reference=str(meta.get("source_reference") or ""),
                    start_line=int(meta.get("start_line") or 0),
                    end_line=int(meta.get("end_line") or 0),
                    row_start=int(meta.get("row_start") or 0),
                    row_end=int(meta.get("row_end") or 0),
                    columns=str(meta.get("columns") or ""),
                    entities=str(meta.get("entities") or ""),
                    year=int(meta.get("year") or 0),
                )
            )
        return evidence

    def create_conversation(self, conversation_id: str) -> dict[str, Any]:
        now = utcnow()
        context = ConversationContext().model_dump_json()
        with self._lock:
            self.conn.execute(
                "INSERT INTO conversations (id, context_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (conversation_id, context, now, now),
            )
            self.conn.commit()
        return {"id": conversation_id, "context_json": context, "created_at": now, "updated_at": now}

    def get_conversation(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            raise NotFoundError("Conversation was not found.")
        return dict(row)

    def update_conversation_context(self, conversation_id: str, context: ConversationContext) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE conversations SET context_json = ?, updated_at = ? WHERE id = ?",
                (context.model_dump_json(), utcnow(), conversation_id),
            )
            self.conn.commit()

    def add_message(self, record: dict[str, Any]) -> None:
        cols = _allowed_columns(record, MESSAGE_COLUMNS)
        placeholders = ", ".join("?" for _ in cols)
        with self._lock:
            self.conn.execute(
                f"INSERT INTO messages ({', '.join(cols)}) VALUES ({placeholders})",
                [record[c] for c in cols],
            )
            self.conn.commit()

    def list_messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_conversation(self, conversation_id: str) -> None:
        self.get_conversation(conversation_id)
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            self.conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self.conn.commit()

    def parse_csv_profile(self, raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        return json.loads(raw)


def _allowed_columns(record: dict[str, Any], allowed: frozenset[str]) -> list[str]:
    cols = [key for key in record if key in allowed]
    if not cols:
        raise StorageError("Invalid storage fields.")
    return cols

