"""SQLite-backed local document metadata catalog."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core.models import DocumentRecord


class DocumentCatalog:
    """Persist document metadata locally; document contents stay in their source files."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL
                )
                """
            )

    def upsert(self, record: DocumentRecord) -> None:
        payload = record.model_dump_json()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO documents(document_id, record_json) VALUES (?, ?) "
                "ON CONFLICT(document_id) DO UPDATE SET record_json=excluded.record_json",
                (record.document_id, payload),
            )

    def get(self, document_id: str) -> DocumentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()
        return DocumentRecord.model_validate_json(row["record_json"]) if row else None

    def list(self) -> list[DocumentRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT record_json FROM documents ORDER BY document_id").fetchall()
        return [DocumentRecord.model_validate_json(row["record_json"]) for row in rows]
