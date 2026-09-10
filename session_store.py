"""SQLite persistence for conversation turns and resolved references."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from app.core.models import SessionTurn


class SessionStore:
    """A lightweight local session store; no conversation data is uploaded by this layer."""

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
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_turns (
                    session_id TEXT NOT NULL,
                    turn_number INTEGER NOT NULL,
                    turn_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, turn_number),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
                """
            )

    def create_session(self) -> str:
        session_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("INSERT INTO sessions(session_id) VALUES (?)", (session_id,))
        return session_id

    def exists(self, session_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone() is not None

    def add_turn(self, session_id: str, turn: SessionTurn) -> None:
        if not self.exists(session_id):
            raise ValueError("The requested session does not exist.")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO session_turns(session_id, turn_number, turn_json) VALUES (?, ?, ?)",
                (session_id, turn.turn_number, turn.model_dump_json()),
            )

    def get_turns(self, session_id: str, limit: int = 8) -> list[SessionTurn]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT turn_json FROM session_turns WHERE session_id = ? "
                "ORDER BY turn_number DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [SessionTurn.model_validate_json(row["turn_json"]) for row in reversed(rows)]

    def next_turn_number(self, session_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(turn_number), 0) AS last_turn FROM session_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["last_turn"]) + 1
