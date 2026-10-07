"""Privacy-aware in-memory events for tests and the deterministic harness."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any


_SENSITIVE_KEYS = {
    "api_key",
    "audio",
    "credentials",
    "instructions",
    "patient_id",
    "payload",
    "phone_number",
    "prompt",
    "token",
    "transcript",
}


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_token")
        or normalized.endswith("_secret")
        or "phone" in normalized
        or normalized.startswith("patient_")
    )


def sanitize_for_log(
    value: Any,
    key: str | None = None,
    *,
    include_transcripts: bool = False,
) -> Any:
    if key and _is_sensitive_key(key) and not (
        include_transcripts and key.lower() == "transcript"
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(k): sanitize_for_log(
                v,
                str(k),
                include_transcripts=include_transcripts,
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            sanitize_for_log(item, include_transcripts=include_transcripts)
            for item in value
        ]
    return value


def _event(event_type: str, metadata: dict[str, Any], *, include_transcripts: bool) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "monotonic_ns": time.monotonic_ns(),
        **sanitize_for_log(metadata, include_transcripts=include_transcripts),
    }


class InMemoryEventSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def record(self, event_type: str, **metadata: Any) -> None:
        self.events.append(_event(event_type, metadata, include_transcripts=False))


class JsonLinesEventSink:
    """Append ordered, bounded, privacy-aware call events to a local JSONL file."""

    def __init__(
        self,
        path: str | Path,
        *,
        include_transcripts: bool = False,
        echo: bool = True,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.include_transcripts = include_transcripts
        self.echo = echo
        self._lock = asyncio.Lock()

    async def record(self, event_type: str, **metadata: Any) -> None:
        event = _event(
            event_type,
            metadata,
            include_transcripts=self.include_transcripts,
        )
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
        async with self._lock:
            await asyncio.to_thread(self._append, encoded)
            if self.echo:
                print(f"VOICE_EVENT {encoded}", flush=True)

    def _append(self, encoded: str) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")


class CompositeEventSink:
    """Fan out telemetry without making one failed sink suppress the others."""

    def __init__(self, *sinks: Any) -> None:
        if not sinks:
            raise ValueError("at least one event sink is required")
        self.sinks = sinks

    async def record(self, event_type: str, **metadata: Any) -> None:
        await asyncio.gather(
            *(sink.record(event_type, **metadata) for sink in self.sinks),
            return_exceptions=True,
        )
