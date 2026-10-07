"""TinyDB document store for synthetic conversation turns and runtime events."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from tinydb import Query, TinyDB


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "confirmation_token",
        "date_of_birth",
        "dob",
        "password",
        "postal_code",
        "secret",
        "token",
    }
)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in _SENSITIVE_KEYS
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class ConversationDocumentStore:
    """Append-oriented NoSQL store isolated from scheduling transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._database = TinyDB(self.path, sort_keys=True, indent=2)
        self._events = self._database.table("conversation_events")

    def close(self) -> None:
        self._database.close()

    def append_turn(
        self,
        *,
        session_id: str,
        role: str,
        content: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        return self.append_event(
            session_id=session_id,
            event_type="conversation.turn",
            occurred_at=occurred_at,
            role=role,
            content=content,
            payload=metadata or {},
        )

    def append_event(
        self,
        *,
        session_id: str,
        event_type: str,
        occurred_at: datetime,
        role: str | None = None,
        content: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> int:
        if not session_id.strip() or not event_type.strip():
            raise ValueError("session_id and event_type are required")
        with self._lock:
            sequence = self._next_sequence(session_id)
            self._events.insert(
                {
                    "session_id": session_id,
                    "sequence": sequence,
                    "event_type": event_type,
                    "role": role,
                    "content": content,
                    "payload": _redact(payload or {}),
                    "occurred_at": occurred_at.isoformat(),
                }
            )
            return sequence

    def events_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            event = Query()
            rows = self._events.search(event.session_id == session_id)
            return sorted(rows, key=lambda row: row["sequence"])

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._events.all())
            rows.sort(key=lambda row: row["occurred_at"], reverse=True)
            return rows[:limit]

    def session_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            summaries: dict[str, dict[str, Any]] = {}
            for row in self._events.all():
                session_id = row["session_id"]
                summary = summaries.setdefault(
                    session_id,
                    {
                        "session_id": session_id,
                        "event_count": 0,
                        "last_event_at": row["occurred_at"],
                        "last_event_type": row["event_type"],
                    },
                )
                summary["event_count"] += 1
                if row["occurred_at"] >= summary["last_event_at"]:
                    summary["last_event_at"] = row["occurred_at"]
                    summary["last_event_type"] = row["event_type"]
            return sorted(
                summaries.values(),
                key=lambda item: item["last_event_at"],
                reverse=True,
            )

    def _next_sequence(self, session_id: str) -> int:
        event = Query()
        rows = self._events.search(event.session_id == session_id)
        return max((int(row["sequence"]) for row in rows), default=0) + 1
