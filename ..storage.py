from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chromadb

from app.config import Settings
from app.errors import ConflictError, NotFoundError, StorageError


class Storage:
    """
    Persistence layer for the document-analysis system.

    SQLite stores:
        - document metadata
        - conversations
        - chat messages

    Chroma stores:
        - embedded document chunks
    """

    COLLECTION_NAME = "doc_chunks"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._conn: sqlite3.Connection | None = None
        self._chroma: Any | None = None
        self._collection: Any | None = None

    # ============================================================
    # INITIALIZATION
    # ============================================================

    def init(self) -> None:
        try:
            self.settings.ensure_directories()

            self._conn = sqlite3.connect(
                self.settings.sqlite_path,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row

            self._conn.execute("PRAGMA foreign_keys = ON")

            self._create_tables()

            self._chroma = chromadb.PersistentClient(
                path=str(self.settings.chroma_path)
            )

            self._collection = self._chroma.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={
                    "hnsw:space": "cosine",
                },
            )

        except Exception as exc:
            self.close()

            if isinstance(exc, StorageError):
                raise

            raise StorageError(
                "Failed to initialize application storage."
            ) from exc

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass

            self._conn = None

        self._collection = None
        self._chroma = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError(
                "Storage has not been initialized."
            )

        return self._conn

    @property
    def collection(self) -> Any:
        if self._collection is None:
            raise StorageError(
                "Vector storage has not been initialized."
            )

        return self._collection

    # ============================================================
    # DATABASE SCHEMA
    # ============================================================

    def _create_tables(self) -> None:
        conn = self.conn

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                page_count INTEGER NOT NULL DEFAULT 0,
                csv_profile TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_documents_status
                ON documents(status);

            CREATE INDEX IF NOT EXISTS idx_documents_filename
                ON documents(filename);

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                context_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                status TEXT,
                sources_json TEXT,
                execution_flow_json TEXT,
                query_plan_json TEXT,
                created_at TEXT NOT NULL,

                FOREIGN KEY (conversation_id)
                    REFERENCES conversations(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages(conversation_id);

            CREATE INDEX IF NOT EXISTS idx_messages_created
                ON messages(created_at);
            """
        )

        conn.commit()

    # ============================================================
    # DOCUMENTS
    # ============================================================

    def insert_document(
        self,
        *,
        document_id: str,
        filename: str,
        stored_name: str,
        file_type: str,
        file_hash: str,
        status: str = "processing",
        chunk_count: int = 0,
        page_count: int = 0,
        csv_profile: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:

        now = _utc_now()

        try:
            self.conn.execute(
                """
                INSERT INTO documents (
                    id,
                    filename,
                    stored_name,
                    file_type,
                    file_hash,
                    status,
                    chunk_count,
                    page_count,
                    csv_profile,
                    error_message,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    filename,
                    stored_name,
                    file_type,
                    file_hash,
                    status,
                    chunk_count,
                    page_count,
                    json.dumps(csv_profile)
                    if csv_profile is not None
                    else None,
                    error_message,
                    now,
                    now,
                ),
            )

            self.conn.commit()

        except sqlite3.IntegrityError as exc:
            self.conn.rollback()

            if "file_hash" in str(exc).lower():
                raise ConflictError(
                    "This document has already been uploaded."
                ) from exc

            raise StorageError(
                "Could not save document metadata."
            ) from exc

        return self.get_document(document_id)

    def update_document(
        self,
        document_id: str,
        *,
        status: str | None = None,
        chunk_count: int | None = None,
        page_count: int | None = None,
        csv_profile: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:

        existing = self.get_document(document_id)

        new_status = (
            status
            if status is not None
            else existing["status"]
        )

        new_chunk_count = (
            chunk_count
            if chunk_count is not None
            else existing["chunk_count"]
        )

        new_page_count = (
            page_count
            if page_count is not None
            else existing["page_count"]
        )

        new_profile = (
            json.dumps(csv_profile)
            if csv_profile is not None
            else existing["csv_profile"]
        )

        new_error = (
            error_message
            if error_message is not None
            else existing["error_message"]
        )

        self.conn.execute(
            """
            UPDATE documents
            SET
                status = ?,
                chunk_count = ?,
                page_count = ?,
                csv_profile = ?,
                error_message = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                new_chunk_count,
                new_page_count,
                new_profile,
                new_error,
                _utc_now(),
                document_id,
            ),
        )

        self.conn.commit()

        return self.get_document(document_id)

    def get_document(
        self,
        document_id: str,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()

        if row is None:
            raise NotFoundError(
                f"Document '{document_id}' was not found."
            )

        return self._document_row(row)

    def get(
        self,
        document_id: str,
    ) -> dict[str, Any]:
        return self.get_document(document_id)

    def get_document_by_hash(
        self,
        file_hash: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM documents
            WHERE file_hash = ?
            """,
            (file_hash,),
        ).fetchone()

        if row is None:
            return None

        return self._document_row(row)

    def list_documents(
        self,
        ready_only: bool = True,
    ) -> list[dict[str, Any]]:

        if ready_only:
            rows = self.conn.execute(
                """
                SELECT *
                FROM documents
                WHERE status = 'ready'
                ORDER BY created_at DESC
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT *
                FROM documents
                ORDER BY created_at DESC
                """
            ).fetchall()

        return [
            self._document_row(row)
            for row in rows
        ]

    def delete_document(
        self,
        document_id: str,
    ) -> None:

        document = self.get_document(document_id)

        self.conn.execute(
            """
            DELETE FROM documents
            WHERE id = ?
            """,
            (document_id,),
        )

        self.conn.commit()

        try:
            self.collection.delete(
                where={
                    "document_id": document_id,
                }
            )
        except Exception as exc:
            # The SQLite metadata has already been removed.
            # Surface vector-store failure so it is not silently hidden.
            raise StorageError(
                f"Failed to remove vector data for "
                f"document '{document_id}'."
            ) from exc

        stored_name = document.get("stored_name")

        if stored_name:
            path = self.settings.upload_dir / stored_name

            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise StorageError(
                    f"Failed to remove stored document '{stored_name}'."
                ) from exc

    def purge_ingest(
        self,
        document_id: str,
    ) -> None:
        """
        Remove all traces of a document after an ingestion failure.
        """

        try:
            self.collection.delete(
                where={
                    "document_id": document_id,
                }
            )
        except Exception:
            pass

        try:
            document = self.get_document(document_id)
        except NotFoundError:
            return

        stored_name = document.get("stored_name")

        self.conn.execute(
            """
            DELETE FROM documents
            WHERE id = ?
            """,
            (document_id,),
        )

        self.conn.commit()

        if stored_name:
            try:
                (
                    self.settings.upload_dir / stored_name
                ).unlink(missing_ok=True)
            except OSError:
                pass

    # ============================================================
    # FILE STORAGE
    # ============================================================

    def upload_path(
        self,
        source_path: Path,
        original_filename: str,
    ) -> tuple[str, str]:
        if not source_path.exists():
            raise NotFoundError(
                "The uploaded file could not be found."
            )

        suffix = source_path.suffix.lower()

        stored_name = (
            f"{uuid.uuid4().hex}{suffix}"
        )

        destination = (
            self.settings.upload_dir / stored_name
        )

        try:
            destination.write_bytes(
                source_path.read_bytes()
            )
        except OSError as exc:
            raise StorageError(
                "Failed to store the uploaded file."
            ) from exc

        file_hash = _sha256(destination)

        return stored_name, file_hash

    def document_file(
        self,
        document_id: str,
    ) -> Path:
        document = self.get_document(document_id)

        path = (
            self.settings.upload_dir
            / document["stored_name"]
        )

        if not path.exists():
            raise NotFoundError(
                f"The stored file for document "
                f"'{document_id}' was not found."
            )

        return path

    # ============================================================
    # CHROMA
    # ============================================================

    def upsert_chunks(
        self,
        *,
        document_id: str,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:

        if len(chunks) != len(embeddings):
            raise StorageError(
                "The number of chunks does not match "
                "the number of embeddings."
            )

        if not chunks:
            return

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for index, chunk in enumerate(chunks):
            chunk_id = str(
                chunk.get("chunk_id")
                or f"{document_id}:{index}"
            )

            text = str(
                chunk.get("text", "")
            )

            metadata = dict(
                chunk.get("metadata") or {}
            )

            metadata["document_id"] = document_id

            ids.append(chunk_id)
            documents.append(text)
            metadatas.append(
                _clean_chroma_metadata(metadata)
            )

        try:
            self.collection.upsert(
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
            )
        except Exception as exc:
            raise StorageError(
                "Failed to store document embeddings."
            ) from exc

    def query_chunks(
        self,
        *,
        embedding: list[float],
        document_ids: list[str] | None = None,
        top_k: int = 8,
    ) -> list[dict[str, Any]]:

        where: dict[str, Any] | None = None

        if document_ids:
            if len(document_ids) == 1:
                where = {
                    "document_id": document_ids[0],
                }
            else:
                where = {
                    "$or": [
                        {"document_id": document_id}
                        for document_id in document_ids
                    ]
                }

        try:
            result = self.collection.query(
                query_embeddings=[embedding],
                n_results=top_k,
                where=where,
                include=[
                    "documents",
                    "metadatas",
                    "distances",
                ],
            )
        except Exception as exc:
            raise StorageError(
                "Failed to query document embeddings."
            ) from exc

        documents = (
            result.get("documents") or [[]]
        )[0]

        metadatas = (
            result.get("metadatas") or [[]]
        )[0]

        distances = (
            result.get("distances") or [[]]
        )[0]

        ids = (
            result.get("ids") or [[]]
        )[0]

        matches: list[dict[str, Any]] = []

        for index, text in enumerate(documents):
            metadata = (
                metadatas[index]
                if index < len(metadatas)
                else {}
            )

            distance = (
                distances[index]
                if index < len(distances)
                else None
            )

            chunk_id = (
                ids[index]
                if index < len(ids)
                else None
            )

            similarity = None

            if distance is not None:
                similarity = 1.0 - float(distance)

            matches.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": metadata.get(
                        "document_id"
                    ),
                    "text": text,
                    "metadata": metadata,
                    "distance": distance,
                    "similarity": similarity,
                }
            )

        return matches

    # ============================================================
    # CONVERSATIONS
    # ============================================================

    def create_conversation(
        self,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:

        conversation_id = (
            conversation_id or str(uuid.uuid4())
        )

        now = _utc_now()

        context = {}

        self.conn.execute(
            """
            INSERT INTO conversations (
                id,
                context_json,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                conversation_id,
                json.dumps(context),
                now,
                now,
            ),
        )

        self.conn.commit()

        return self.get_conversation(
            conversation_id
        )

    def get_conversation(
        self,
        conversation_id: str,
    ) -> dict[str, Any]:

        row = self.conn.execute(
            """
            SELECT *
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()

        if row is None:
            raise NotFoundError(
                f"Conversation '{conversation_id}' was not found."
            )

        return dict(row)

    def update_conversation_context(
        self,
        conversation_id: str,
        context: Any,
    ) -> None:

        self.get_conversation(conversation_id)

        if hasattr(context, "model_dump"):
            payload = context.model_dump(
                mode="json",
                by_alias=True,
            )
        elif isinstance(context, dict):
            payload = context
        else:
            raise StorageError(
                "Conversation context must be a dictionary "
                "or a Pydantic model."
            )

        self.conn.execute(
            """
            UPDATE conversations
            SET
                context_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(payload),
                _utc_now(),
                conversation_id,
            ),
        )

        self.conn.commit()

    # ============================================================
    # MESSAGES
    # ============================================================

    def insert_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        status: str | None = None,
        sources: list[dict[str, Any]] | None = None,
        execution_flow: list[str] | None = None,
        query_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:

        self.get_conversation(conversation_id)

        message_id = str(uuid.uuid4())

        self.conn.execute(
            """
            INSERT INTO messages (
                id,
                conversation_id,
                role,
                content,
                status,
                sources_json,
                execution_flow_json,
                query_plan_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                role,
                content,
                status,
                json.dumps(sources or []),
                json.dumps(execution_flow or []),
                json.dumps(query_plan)
                if query_plan is not None
                else None,
                _utc_now(),
            ),
        )

        self.conn.execute(
            """
            UPDATE conversations
            SET updated_at = ?
            WHERE id = ?
            """,
            (
                _utc_now(),
                conversation_id,
            ),
        )

        self.conn.commit()

        return self.get_message(message_id)

    def get_message(
        self,
        message_id: str,
    ) -> dict[str, Any]:

        row = self.conn.execute(
            """
            SELECT *
            FROM messages
            WHERE id = ?
            """,
            (message_id,),
        ).fetchone()

        if row is None:
            raise NotFoundError(
                f"Message '{message_id}' was not found."
            )

        return self._message_row(row)

    def list_messages(
        self,
        conversation_id: str,
    ) -> list[dict[str, Any]]:

        self.get_conversation(conversation_id)

        rows = self.conn.execute(
            """
            SELECT *
            FROM messages
            WHERE conversation_id = ?
            ORDER BY created_at ASC
            """,
            (conversation_id,),
        ).fetchall()

        return [
            self._message_row(row)
            for row in rows
        ]

    # ============================================================
    # HELPERS
    # ============================================================

    @staticmethod
    def _document_row(
        row: sqlite3.Row,
    ) -> dict[str, Any]:

        result = dict(row)

        if result.get("csv_profile"):
            try:
                result["csv_profile"] = json.loads(
                    result["csv_profile"]
                )
            except (json.JSONDecodeError, TypeError):
                result["csv_profile"] = {}

        return result

    @staticmethod
    def _message_row(
        row: sqlite3.Row,
    ) -> dict[str, Any]:

        result = dict(row)

        for key in (
            "sources_json",
            "execution_flow_json",
            "query_plan_json",
        ):
            value = result.get(key)

            if value is None:
                continue

            try:
                result[key.replace("_json", "")] = (
                    json.loads(value)
                )
            except (json.JSONDecodeError, TypeError):
                result[key.replace("_json", "")] = (
                    [] if key != "query_plan_json"
                    else None
                )

        return result

    @staticmethod
    def parse_csv_profile(
        value: Any,
    ) -> dict[str, Any]:

        if value is None:
            return {}

        if isinstance(value, dict):
            return value

        if isinstance(value, str):
            try:
                parsed = json.loads(value)

                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        return {}


# ================================================================
# MODULE HELPERS
# ================================================================


def _utc_now() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    try:
        with path.open("rb") as file:
            while True:
                block = file.read(1024 * 1024)

                if not block:
                    break

                digest.update(block)

    except OSError as exc:
        raise StorageError(
            "Could not calculate the file hash."
        ) from exc

    return digest.hexdigest()


def _clean_chroma_metadata(
    metadata: dict[str, Any],
) -> dict[str, Any]:

    cleaned: dict[str, Any] = {}

    for key, value in metadata.items():

        if value is None:
            continue

        if isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
            continue

        if isinstance(value, list):
            cleaned[key] = ", ".join(
                str(item)
                for item in value
            )
            continue

        cleaned[key] = str(value)

    return cleaned
