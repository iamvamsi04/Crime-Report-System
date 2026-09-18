from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import chromadb

from app.config import Settings
from app.errors import NotFoundError, StorageError
from app.models import ConversationContext, Evidence

log = logging.getLogger(__name__)


CHROMA_COLLECTION_NAME = "document_chunks"


def utcnow() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


class Storage:
    """
    Application persistence.

    SQLite:
        document metadata
        conversation state
        chat messages

    Chroma:
        semantic retrieval chunks

    Filesystem:
        original uploaded files

    Structured analysis always reloads the original file from the filesystem;
    Chroma is never treated as the source of truth for CSV/Excel values.
    """

    def __init__(
        self,
        settings: Settings,
    ) -> None:
        self.settings = settings

        self.sqlite_path = (
            settings.sqlite_path
        )

        self.chroma_path = (
            settings.chroma_path
        )

        self.upload_dir = (
            settings.upload_path
        )

        self._write_lock = (
            threading.RLock()
        )

        self._chroma_client: Any | None = None
        self._collection: Any | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(
        self,
    ) -> None:
        try:
            self.sqlite_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.chroma_path.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.upload_dir.mkdir(
                parents=True,
                exist_ok=True,
            )

            with self._connection() as conn:
                self._create_schema(
                    conn
                )

            self._initialize_chroma()

        except Exception as exc:
            if isinstance(
                exc,
                StorageError,
            ):
                raise

            raise StorageError(
                "Storage could not be initialized."
            ) from exc

    def close(
        self,
    ) -> None:
        """
        PersistentClient does not require an explicit close in the common
        Chroma API. Drop references so shutdown/reload is clean.
        """

        self._collection = None
        self._chroma_client = None

    # ------------------------------------------------------------------
    # Files
    # ------------------------------------------------------------------

    def upload_path(
        self,
        stored_name: str,
    ) -> Path:
        """
        Return a safe path inside the configured upload directory.
        """

        safe_name = Path(
            stored_name
        ).name

        if (
            not safe_name
            or safe_name != stored_name
        ):
            raise StorageError(
                "Invalid stored filename."
            )

        return (
            self.upload_dir
            / safe_name
        )

    def document_file(
        self,
        document_id: str,
    ) -> Path:
        """
        Resolve the original uploaded file for a document.

        This is the boundary used by dataset.py. Physical paths never need to
        be shown to Gemini.
        """

        document = self.get_document(
            document_id
        )

        if document is None:
            raise NotFoundError(
                "Document not found."
            )

        stored_name = str(
            document.get(
                "stored_name"
            )
            or ""
        )

        if not stored_name:
            raise StorageError(
                "The document has no stored file."
            )

        path = self.upload_path(
            stored_name
        )

        if not path.is_file():
            raise StorageError(
                "The document file is missing from storage."
            )

        return path

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def create_document(
        self,
        document: dict[str, Any],
    ) -> dict[str, Any]:
        required = {
            "id",
            "filename",
            "stored_name",
            "file_type",
            "file_hash",
            "status",
        }

        missing = required.difference(
            document
        )

        if missing:
            raise StorageError(
                "Document metadata is incomplete."
            )

        try:
            with self._write_lock:
                with self._connection() as conn:
                    conn.execute(
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
                            document["id"],
                            document["filename"],
                            document[
                                "stored_name"
                            ],
                            document[
                                "file_type"
                            ],
                            document[
                                "file_hash"
                            ],
                            document[
                                "status"
                            ],
                            int(
                                document.get(
                                    "chunk_count"
                                )
                                or 0
                            ),
                            document.get(
                                "page_count"
                            ),
                            _serialize_jsonish(
                                document.get(
                                    "csv_profile"
                                )
                            ),
                            document.get(
                                "error_message"
                            ),
                            document.get(
                                "created_at"
                            )
                            or utcnow(),
                            document.get(
                                "updated_at"
                            )
                            or utcnow(),
                        ),
                    )

        except sqlite3.IntegrityError as exc:
            raise StorageError(
                "Document metadata already exists."
            ) from exc

        except Exception as exc:
            raise StorageError(
                "Document metadata could not be stored."
            ) from exc

        created = self.get_document(
            str(
                document["id"]
            )
        )

        if created is None:
            raise StorageError(
                "Stored document could not be reloaded."
            )

        return created

    def update_document(
        self,
        document_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = {
            "filename",
            "stored_name",
            "file_type",
            "file_hash",
            "status",
            "chunk_count",
            "page_count",
            "csv_profile",
            "error_message",
            "updated_at",
        }

        values = {
            key: value
            for key, value in updates.items()
            if key in allowed
        }

        if not values:
            document = self.get_document(
                document_id
            )

            if document is None:
                raise NotFoundError(
                    "Document not found."
                )

            return document

        values["updated_at"] = (
            values.get(
                "updated_at"
            )
            or utcnow()
        )

        if "csv_profile" in values:
            values["csv_profile"] = (
                _serialize_jsonish(
                    values[
                        "csv_profile"
                    ]
                )
            )

        assignments = ", ".join(
            f"{column} = ?"
            for column in values
        )

        parameters = [
            values[column]
            for column in values
        ]

        parameters.append(
            document_id
        )

        try:
            with self._write_lock:
                with self._connection() as conn:
                    cursor = conn.execute(
                        f"""
                        UPDATE documents
                        SET {assignments}
                        WHERE id = ?
                        """,
                        parameters,
                    )

                    if cursor.rowcount == 0:
                        raise NotFoundError(
                            "Document not found."
                        )

        except NotFoundError:
            raise

        except Exception as exc:
            raise StorageError(
                "Document metadata could not be updated."
            ) from exc

        document = self.get_document(
            document_id
        )

        if document is None:
            raise NotFoundError(
                "Document not found."
            )

        return document

    def get_document(
        self,
        document_id: str,
    ) -> dict[str, Any] | None:
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM documents
                    WHERE id = ?
                    """,
                    (
                        document_id,
                    ),
                ).fetchone()

        except Exception as exc:
            raise StorageError(
                "Document metadata could not be read."
            ) from exc

        return (
            _row_dict(row)
            if row is not None
            else None
        )

    def get_document_by_hash(
        self,
        file_hash: str,
    ) -> dict[str, Any] | None:
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM documents
                    WHERE file_hash = ?
                    LIMIT 1
                    """,
                    (
                        file_hash,
                    ),
                ).fetchone()

        except Exception as exc:
            raise StorageError(
                "Document metadata could not be read."
            ) from exc

        return (
            _row_dict(row)
            if row is not None
            else None
        )

    def list_documents(
        self,
        *,
        ready_only: bool = False,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT *
            FROM documents
        """

        parameters: tuple[
            Any,
            ...
        ] = ()

        if ready_only:
            sql += """
                WHERE status = ?
            """

            parameters = (
                "ready",
            )

        sql += """
            ORDER BY created_at ASC
        """

        try:
            with self._connection() as conn:
                rows = conn.execute(
                    sql,
                    parameters,
                ).fetchall()

        except Exception as exc:
            raise StorageError(
                "Documents could not be listed."
            ) from exc

        return [
            _row_dict(row)
            for row in rows
        ]

    def delete_document(
        self,
        document_id: str,
    ) -> dict[str, Any]:
        document = self.get_document(
            document_id
        )

        if document is None:
            raise NotFoundError(
                "Document not found."
            )

        # Delete vector evidence before metadata. If Chroma fails, preserve the
        # DB record/file rather than creating an invisible orphan.
        self.delete_document_chunks(
            document_id
        )

        try:
            with self._write_lock:
                with self._connection() as conn:
                    conn.execute(
                        """
                        DELETE FROM documents
                        WHERE id = ?
                        """,
                        (
                            document_id,
                        ),
                    )

        except Exception as exc:
            raise StorageError(
                "Document metadata could not be deleted."
            ) from exc

        stored_name = str(
            document.get(
                "stored_name"
            )
            or ""
        )

        if stored_name:
            try:
                self.upload_path(
                    stored_name
                ).unlink(
                    missing_ok=True
                )
            except Exception:
                log.exception(
                    "document_file_delete_failed "
                    "document_id=%s",
                    document_id,
                )

        return document

    # ------------------------------------------------------------------
    # Chroma
    # ------------------------------------------------------------------

    def upsert_chunks(
        self,
        *,
        chunks: list[dict[str, Any]],
        embeddings: list[list[float]],
    ) -> None:
        if not chunks:
            return

        if len(chunks) != len(
            embeddings
        ):
            raise StorageError(
                "Chunk and embedding counts do not match."
            )

        collection = self._get_collection()

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[
            dict[str, Any]
        ] = []

        for chunk in chunks:
            chunk_id = str(
                chunk.get("id")
                or ""
            )

            if not chunk_id:
                raise StorageError(
                    "A chunk is missing its ID."
                )

            ids.append(
                chunk_id
            )

            documents.append(
                str(
                    chunk.get(
                        "text"
                    )
                    or ""
                )
            )

            metadatas.append(
                _chunk_metadata(
                    chunk
                )
            )

        try:
            collection.upsert(
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metadatas,
            )

        except Exception as exc:
            raise StorageError(
                "Document chunks could not be stored."
            ) from exc

    def query_chunks(
        self,
        *,
        query_embedding: list[float],
        top_k: int,
        document_ids: list[str] | None = None,
    ) -> list[Evidence]:
        if not query_embedding:
            return []

        if top_k <= 0:
            return []

        collection = self._get_collection()

        where = _document_where(
            document_ids
        )

        kwargs: dict[
            str,
            Any,
        ] = {
            "query_embeddings": [
                query_embedding
            ],
            "n_results": int(
                top_k
            ),
            "include": [
                "documents",
                "metadatas",
                "distances",
            ],
        }

        if where is not None:
            kwargs["where"] = where

        try:
            result = collection.query(
                **kwargs
            )

        except Exception as exc:
            raise StorageError(
                "Document evidence could not be retrieved."
            ) from exc

        ids = _first_result_list(
            result.get(
                "ids"
            )
        )

        documents = _first_result_list(
            result.get(
                "documents"
            )
        )

        metadatas = _first_result_list(
            result.get(
                "metadatas"
            )
        )

        distances = _first_result_list(
            result.get(
                "distances"
            )
        )

        evidence: list[
            Evidence
        ] = []

        count = max(
            len(ids),
            len(documents),
            len(metadatas),
            len(distances),
        )

        for index in range(
            count
        ):
            metadata = (
                metadatas[index]
                if index
                < len(metadatas)
                and isinstance(
                    metadatas[
                        index
                    ],
                    dict,
                )
                else {}
            )

            text = (
                str(
                    documents[
                        index
                    ]
                )
                if index
                < len(documents)
                and documents[
                    index
                ]
                is not None
                else ""
            )

            distance = (
                distances[
                    index
                ]
                if index
                < len(distances)
                else None
            )

            similarity = (
                _distance_to_similarity(
                    distance
                )
            )

            evidence.append(
                Evidence(
                    document_id=str(
                        metadata.get(
                            "document_id"
                        )
                        or ""
                    ),
                    filename=str(
                        metadata.get(
                            "filename"
                        )
                        or ""
                    ),
                    document_type=str(
                        metadata.get(
                            "document_type"
                        )
                        or ""
                    ),
                    chunk_id=(
                        str(
                            ids[
                                index
                            ]
                        )
                        if index
                        < len(ids)
                        and ids[
                            index
                        ]
                        is not None
                        else None
                    ),
                    text=text,
                    similarity=similarity,
                    source_reference=_none_if_empty(
                        metadata.get(
                            "source_reference"
                        )
                    ),
                    page_number=_optional_int(
                        metadata.get(
                            "page_number"
                        )
                    ),
                    section=_none_if_empty(
                        metadata.get(
                            "section"
                        )
                    ),
                    start_line=_optional_int(
                        metadata.get(
                            "start_line"
                        )
                    ),
                    end_line=_optional_int(
                        metadata.get(
                            "end_line"
                        )
                    ),
                    row_start=_optional_int(
                        metadata.get(
                            "row_start"
                        )
                    ),
                    row_end=_optional_int(
                        metadata.get(
                            "row_end"
                        )
                    ),
                )
            )

        return evidence

    def delete_document_chunks(
        self,
        document_id: str,
    ) -> None:
        collection = self._get_collection()

        try:
            collection.delete(
                where={
                    "document_id": (
                        document_id
                    )
                }
            )

        except Exception as exc:
            raise StorageError(
                "Document chunks could not be deleted."
            ) from exc

    # ------------------------------------------------------------------
    # Conversations
    # ------------------------------------------------------------------

    def create_conversation(
        self,
        conversation_id: str,
    ) -> dict[str, Any]:
        now = utcnow()

        context = (
            ConversationContext()
        )

        try:
            with self._write_lock:
                with self._connection() as conn:
                    conn.execute(
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
                            context.model_dump_json(),
                            now,
                            now,
                        ),
                    )

        except sqlite3.IntegrityError:
            existing = (
                self.get_conversation(
                    conversation_id
                )
            )

            if existing is not None:
                return existing

            raise StorageError(
                "Conversation could not be created."
            )

        except Exception as exc:
            raise StorageError(
                "Conversation could not be created."
            ) from exc

        conversation = (
            self.get_conversation(
                conversation_id
            )
        )

        if conversation is None:
            raise StorageError(
                "Conversation could not be reloaded."
            )

        return conversation

    def get_conversation(
        self,
        conversation_id: str,
    ) -> dict[str, Any] | None:
        try:
            with self._connection() as conn:
                row = conn.execute(
                    """
                    SELECT *
                    FROM conversations
                    WHERE id = ?
                    """,
                    (
                        conversation_id,
                    ),
                ).fetchone()

        except Exception as exc:
            raise StorageError(
                "Conversation could not be read."
            ) from exc

        return (
            _row_dict(row)
            if row is not None
            else None
        )

    def update_conversation_context(
        self,
        conversation_id: str,
        context: ConversationContext,
    ) -> None:
        try:
            with self._write_lock:
                with self._connection() as conn:
                    cursor = conn.execute(
                        """
                        UPDATE conversations
                        SET
                            context_json = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            context.model_dump_json(),
                            utcnow(),
                            conversation_id,
                        ),
                    )

                    if cursor.rowcount == 0:
                        raise NotFoundError(
                            "Conversation not found."
                        )

        except NotFoundError:
            raise

        except Exception as exc:
            raise StorageError(
                "Conversation context could not be updated."
            ) from exc

    def delete_conversation(
        self,
        conversation_id: str,
    ) -> None:
        conversation = (
            self.get_conversation(
                conversation_id
            )
        )

        if conversation is None:
            raise NotFoundError(
                "Conversation not found."
            )

        try:
            with self._write_lock:
                with self._connection() as conn:
                    conn.execute(
                        """
                        DELETE FROM conversations
                        WHERE id = ?
                        """,
                        (
                            conversation_id,
                        ),
                    )

        except Exception as exc:
            raise StorageError(
                "Conversation could not be deleted."
            ) from exc

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def add_message(
        self,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        required = {
            "id",
            "conversation_id",
            "role",
            "content",
        }

        missing = required.difference(
            message
        )

        if missing:
            raise StorageError(
                "Message metadata is incomplete."
            )

        try:
            with self._write_lock:
                with self._connection() as conn:
                    conn.execute(
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
                            message["id"],
                            message[
                                "conversation_id"
                            ],
                            message[
                                "role"
                            ],
                            message[
                                "content"
                            ],
                            message.get(
                                "status"
                            ),
                            _serialize_jsonish(
                                message.get(
                                    "sources_json"
                                )
                            ),
                            _serialize_jsonish(
                                message.get(
                                    "execution_flow_json"
                                )
                            ),
                            _serialize_jsonish(
                                message.get(
                                    "query_plan_json"
                                )
                            ),
                            message.get(
                                "created_at"
                            )
                            or utcnow(),
                        ),
                    )

        except sqlite3.IntegrityError as exc:
            raise StorageError(
                "Message could not be stored."
            ) from exc

        except Exception as exc:
            raise StorageError(
                "Message could not be stored."
            ) from exc

        return dict(
            message
        )

    def list_messages(
        self,
        conversation_id: str,
    ) -> list[dict[str, Any]]:
        try:
            with self._connection() as conn:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (
                        conversation_id,
                    ),
                ).fetchall()

        except Exception as exc:
            raise StorageError(
                "Conversation messages could not be read."
            ) from exc

        return [
            _row_dict(row)
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Internal SQLite
    # ------------------------------------------------------------------

    @contextmanager
    def _connection(
        self,
    ) -> Iterator[
        sqlite3.Connection
    ]:
        connection: (
            sqlite3.Connection
            | None
        ) = None

        try:
            connection = (
                sqlite3.connect(
                    str(
                        self.sqlite_path
                    ),
                    timeout=30.0,
                )
            )

            connection.row_factory = (
                sqlite3.Row
            )

            connection.execute(
                "PRAGMA foreign_keys = ON"
            )

            connection.execute(
                "PRAGMA busy_timeout = 30000"
            )

            yield connection

            connection.commit()

        except Exception:
            if connection is not None:
                connection.rollback()

            raise

        finally:
            if connection is not None:
                connection.close()

    def _create_schema(
        self,
        conn: sqlite3.Connection,
    ) -> None:
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
                page_count INTEGER,
                csv_profile TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_documents_status
            ON documents(status);

            CREATE INDEX IF NOT EXISTS idx_documents_created_at
            ON documents(created_at);

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                context_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
            ON conversations(updated_at);

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
                FOREIGN KEY (
                    conversation_id
                )
                REFERENCES conversations(id)
                ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(
                conversation_id,
                created_at
            );
            """
        )

    # ------------------------------------------------------------------
    # Internal Chroma
    # ------------------------------------------------------------------

    def _initialize_chroma(
        self,
    ) -> None:
        try:
            self._chroma_client = (
                chromadb.PersistentClient(
                    path=str(
                        self.chroma_path
                    )
                )
            )

            self._collection = (
                self._chroma_client
                .get_or_create_collection(
                    name=(
                        CHROMA_COLLECTION_NAME
                    ),
                    metadata={
                        "hnsw:space": (
                            "cosine"
                        )
                    },
                )
            )

        except Exception as exc:
            raise StorageError(
                "Vector storage could not be initialized."
            ) from exc

    def _get_collection(
        self,
    ) -> Any:
        if self._collection is None:
            self._initialize_chroma()

        if self._collection is None:
            raise StorageError(
                "Vector storage is unavailable."
            )

        return self._collection


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _row_dict(
    row: sqlite3.Row,
) -> dict[str, Any]:
    return {
        key: row[key]
        for key in row.keys()
    }


def _serialize_jsonish(
    value: Any,
) -> Any:
    """
    SQLite fields such as csv_profile/sources_json may already be serialized
    strings. Avoid double encoding them.
    """

    if value is None:
        return None

    if isinstance(
        value,
        str,
    ):
        return value

    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
    )


def _chunk_metadata(
    chunk: dict[str, Any],
) -> dict[str, Any]:
    """
    Chroma metadata values should remain scalar.

    Optional values are omitted rather than stored as None because Chroma
    versions differ in whether None metadata is accepted.
    """

    metadata: dict[
        str,
        Any,
    ] = {
        "document_id": str(
            chunk.get(
                "document_id"
            )
            or ""
        ),
        "filename": str(
            chunk.get(
                "filename"
            )
            or ""
        ),
        "document_type": str(
            chunk.get(
                "document_type"
            )
            or ""
        ),
    }

    optional = {
        "source_reference": (
            chunk.get(
                "source_reference"
            )
        ),
        "page_number": (
            chunk.get(
                "page_number"
            )
        ),
        "section": (
            chunk.get(
                "section"
            )
        ),
        "start_line": (
            chunk.get(
                "start_line"
            )
        ),
        "end_line": (
            chunk.get(
                "end_line"
            )
        ),
        "row_start": (
            chunk.get(
                "row_start"
            )
        ),
        "row_end": (
            chunk.get(
                "row_end"
            )
        ),
    }

    for key, value in optional.items():
        if value is None:
            continue

        if isinstance(
            value,
            bool,
        ):
            metadata[key] = value
            continue

        if isinstance(
            value,
            (int, float),
        ):
            metadata[key] = value
            continue

        metadata[key] = str(
            value
        )

    return metadata


def _document_where(
    document_ids: list[str] | None,
) -> dict[str, Any] | None:
    if not document_ids:
        return None

    cleaned = list(
        dict.fromkeys(
            str(document_id)
            for document_id
            in document_ids
            if str(
                document_id
            ).strip()
        )
    )

    if not cleaned:
        return None

    if len(cleaned) == 1:
        return {
            "document_id": (
                cleaned[0]
            )
        }

    return {
        "document_id": {
            "$in": cleaned
        }
    }


def _first_result_list(
    value: Any,
) -> list[Any]:
    """
    Chroma query results are normally shaped as:
        [[item1, item2, ...]]

    This helper tolerates empty/missing results.
    """

    if value is None:
        return []

    if not isinstance(
        value,
        list,
    ):
        return []

    if not value:
        return []

    first = value[0]

    if isinstance(
        first,
        list,
    ):
        return first

    # Defensive fallback for a flat response shape.
    return value


def _distance_to_similarity(
    distance: Any,
) -> float | None:
    if distance is None:
        return None

    try:
        value = float(
            distance
        )
    except (
        TypeError,
        ValueError,
    ):
        return None

    # Collection uses cosine distance:
    #     distance = 1 - cosine_similarity
    similarity = (
        1.0 - value
    )

    # Numerical implementations can occasionally drift slightly.
    return max(
        -1.0,
        min(
            1.0,
            similarity,
        ),
    )


def _optional_int(
    value: Any,
) -> int | None:
    if value is None:
        return None

    if isinstance(
        value,
        bool,
    ):
        return None

    try:
        return int(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return None


def _none_if_empty(
    value: Any,
) -> str | None:
    if value is None:
        return None

    text = str(
        value
    ).strip()

    return (
        text
        if text
        else None
    )
