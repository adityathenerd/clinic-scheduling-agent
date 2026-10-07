"""Typed values shared by call mechanics and provider adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


NORMAL_TURN_PROGRESS_TIMEOUT = 5.0


class CallDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SessionState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSED = "closed"
    FAILED = "failed"


class OperationKind(str, Enum):
    READ = "read"
    WRITE = "write"


class ToolStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class VoiceArchitecture(str, Enum):
    """Provider-neutral execution shape behind the VoiceEngine facade."""

    LIVE_DELEGATED = "live_delegated"
    REALTIME_DIRECT = "realtime_direct"


class NormalizedEventKind(str, Enum):
    """Stable stages used by the call actor, harness, and latency reports."""

    LIVE_SESSION_STARTED = "live.session_started"
    DELEGATION_CREATED = "delegation.created"
    BACKEND_RESPONSE_STARTED = "backend.response_started"
    BACKEND_TOOL_REQUESTED = "backend.tool_requested"
    BACKEND_TOOL_COMPLETED = "backend.tool_completed"
    DELEGATION_COMPLETED = "delegation.completed"
    DELEGATION_CANCELLED = "delegation.cancelled"
    DELEGATION_STALE = "delegation.stale"
    LIVE_COMMENTARY_APPENDED = "live.commentary_appended"
    LIVE_SESSION_CLOSED = "live.session_closed"


@dataclass(frozen=True, slots=True)
class CallDescriptor:
    provider_call_id: str
    direction: CallDirection
    destination: str | None = None
    fallback_destination: str | None = None
    context_reference: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_call_id.strip():
            raise ValueError("provider_call_id is required")
        if self.direction is CallDirection.OUTBOUND and not self.destination:
            raise ValueError("outbound calls require a destination")


@dataclass(frozen=True, slots=True)
class AudioChunk:
    payload: bytes
    sequence: int
    timestamp_ms: int

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.timestamp_ms < 0:
            raise ValueError("audio sequence and timestamp must be non-negative")


@dataclass(frozen=True, slots=True)
class VoiceSessionConfig:
    instructions: str
    backend_instructions: str | None = None
    initial_commentary: str | None = None
    tools: tuple[Mapping[str, Any], ...] = ()
    context_reference: str | None = None
    architecture: VoiceArchitecture = VoiceArchitecture.LIVE_DELEGATED
    frontend_model: str = "gpt-live-1"
    backend_model: str = "gpt-6-sol"
    backend_reasoning_effort: str = "low"


@dataclass(frozen=True, slots=True)
class ToolIntent:
    operation_id: str
    tool_name: str
    operation_kind: OperationKind
    arguments: Mapping[str, Any] = field(default_factory=dict)
    delegation_id: str | None = None
    state_revision: int | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or not self.tool_name:
            raise ValueError("tool intent requires operation_id and tool_name")


@dataclass(frozen=True, slots=True)
class ToolResult:
    operation_id: str
    status: ToolStatus
    payload: Mapping[str, Any] = field(default_factory=dict)
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class SessionEvent:
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)
