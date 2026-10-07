"""Supervisor for deduplicated, isolated, parallel call actors."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from .actor import CallSessionActor
from .models import CallDescriptor


@dataclass(slots=True)
class _SessionEntry:
    actor: CallSessionActor
    start_task: asyncio.Task[CallSessionActor]


class CallSupervisor:
    def __init__(self, actor_factory: Callable[[CallDescriptor], CallSessionActor]) -> None:
        self._actor_factory = actor_factory
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    async def accept(self, descriptor: CallDescriptor) -> tuple[CallSessionActor, bool]:
        async with self._lock:
            existing = self._sessions.get(descriptor.provider_call_id)
            if existing is not None:
                if existing.actor.descriptor != descriptor:
                    raise ValueError("duplicate provider_call_id has conflicting call metadata")
                entry = existing
                created = False
            else:
                actor = self._actor_factory(descriptor)
                entry = _SessionEntry(actor=actor, start_task=asyncio.create_task(actor.start()))
                self._sessions[descriptor.provider_call_id] = entry
                created = True

        try:
            actor = await entry.start_task
        except Exception:
            async with self._lock:
                if self._sessions.get(descriptor.provider_call_id) is entry:
                    self._sessions.pop(descriptor.provider_call_id, None)
            raise

        if created:
            asyncio.create_task(self._remove_when_done(descriptor.provider_call_id, entry))
        return actor, created

    async def _remove_when_done(self, call_id: str, entry: _SessionEntry) -> None:
        await entry.actor.done.wait()
        async with self._lock:
            if self._sessions.get(call_id) is entry:
                self._sessions.pop(call_id, None)

    async def close_all(self, reason: str = "supervisor_shutdown") -> None:
        async with self._lock:
            actors = [entry.actor for entry in self._sessions.values()]
        await asyncio.gather(*(actor.close(reason) for actor in actors), return_exceptions=True)
