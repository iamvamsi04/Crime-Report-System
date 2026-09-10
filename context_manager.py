"""Creates compact, source-aware context for resolving follow-up questions."""

from __future__ import annotations

from app.conversation.session_store import SessionStore
from app.core.models import SessionTurn


class ContextManager:
    """Keep a concise local memory of entities, rankings, and cited sources per session."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def get_or_create_session(self, session_id: str | None) -> str:
        if session_id:
            if not self.store.exists(session_id):
                raise ValueError("The requested session does not exist.")
            return session_id
        return self.store.create_session()

    def context_for_planning(self, session_id: str) -> str:
        turns = self.store.get_turns(session_id, limit=5)
        if not turns:
            return "No earlier turn exists in this session."
        sections: list[str] = []
        for turn in turns:
            entities = ", ".join(f"{key}={value}" for key, value in turn.resolved_entities.items()) or "none"
            sections.append(
                f"Turn {turn.turn_number}: question={turn.question!r}; summary={turn.summary!r}; "
                f"resolved_entities={entities}; sources={', '.join(turn.cited_source_ids) or 'none'}"
            )
        return "\n".join(sections)

    def record_turn(
        self,
        session_id: str,
        question: str,
        answer: str,
        resolved_entities: dict[str, str],
        cited_source_ids: list[str],
    ) -> None:
        summary = answer.replace("\n", " ").strip()[:600]
        turn = SessionTurn(
            turn_number=self.store.next_turn_number(session_id),
            question=question,
            answer=answer,
            summary=summary,
            resolved_entities=resolved_entities,
            cited_source_ids=cited_source_ids,
        )
        self.store.add_turn(session_id, turn)

    def history(self, session_id: str) -> list[SessionTurn]:
        if not self.store.exists(session_id):
            raise ValueError("The requested session does not exist.")
        return self.store.get_turns(session_id, limit=100)
