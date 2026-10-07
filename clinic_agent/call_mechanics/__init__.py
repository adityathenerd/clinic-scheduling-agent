"""Async call lifecycle primitives.

The package intentionally contains no vendor SDK code. Provider adapters implement
the protocols defined here while the actor and supervisor retain lifecycle ownership.
"""

from .actor import CallSessionActor
from .event_sink import CompositeEventSink, InMemoryEventSink, JsonLinesEventSink
from .models import (
    AudioChunk,
    CallDescriptor,
    CallDirection,
    NormalizedEventKind,
    OperationKind,
    SessionEvent,
    SessionState,
    ToolIntent,
    ToolResult,
    ToolStatus,
    VoiceArchitecture,
    VoiceSessionConfig,
)
from .queue import BoundedAsyncQueue, QueueClosedError, QueueOverflowError
from .supervisor import CallSupervisor
from .worker_pool import AsyncWorkerPool

__all__ = [
    "AsyncWorkerPool",
    "AudioChunk",
    "BoundedAsyncQueue",
    "CallDescriptor",
    "CallDirection",
    "CallSessionActor",
    "CallSupervisor",
    "CompositeEventSink",
    "InMemoryEventSink",
    "JsonLinesEventSink",
    "NormalizedEventKind",
    "OperationKind",
    "QueueClosedError",
    "QueueOverflowError",
    "SessionEvent",
    "SessionState",
    "ToolIntent",
    "ToolResult",
    "ToolStatus",
    "VoiceArchitecture",
    "VoiceSessionConfig",
]
