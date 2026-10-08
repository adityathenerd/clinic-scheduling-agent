"""Provider-neutral contracts consumed by the call-session actor."""

from __future__ import annotations

from typing import Any, Protocol

from .models import (
    AudioChunk,
    CallDescriptor,
    SpeechClip,
    ToolIntent,
    ToolResult,
    VoiceSessionConfig,
)


class TelephonyAdapter(Protocol):
    async def send_audio(self, chunk: AudioChunk) -> None: ...

    async def clear_playback(self) -> None: ...

    async def mark_playback(self, label: str) -> None: ...

    async def transfer(self, destination: str) -> None: ...

    async def close(self, reason: str) -> None: ...


class LiveFrontend(Protocol):
    """Full-duplex speech, turn-taking, and caller-heard output."""

    async def connect(self, config: VoiceSessionConfig) -> None: ...

    async def push_audio(self, chunk: AudioChunk) -> None: ...

    async def cancel_response(self) -> None: ...

    async def truncate_response(self, item_id: str, audio_end_ms: int) -> None: ...

    async def append_commentary(self, content: str) -> None: ...

    async def append_thinking(self, content: str) -> None:
        """Add quiet context that the live model must not speak on append."""
        ...

    async def append_instructions(self, content: str) -> int | None:
        """Append instructions and return the first audio sequence they may own."""
        ...

    async def close(self) -> None: ...


class DelegatedBackend(Protocol):
    """Task reasoning lifecycle behind the live conversational frontend."""

    async def cancel_delegation(self, delegation_id: str, reason: str) -> None: ...

    async def submit_tool_result(self, result: ToolResult) -> None: ...


class VoiceEngine(LiveFrontend, DelegatedBackend, Protocol):
    """Facade implemented by GPT-Live and by the direct Realtime fallback.

    A direct Realtime adapter implements ``cancel_delegation`` as a safe no-op;
    this keeps the application actor and control-plane contract unchanged.
    """


class SpeechRenderer(Protocol):
    """Render application-owned wording into telephony-ready audio."""

    async def render(self, text: str) -> SpeechClip: ...


class AgentControlPlane(Protocol):
    def begin_caller_turn(self, turn_epoch: int) -> None: ...

    @property
    def normal_conversation_active(self) -> bool: ...

    @property
    def application_owns_completed_turn(self) -> bool: ...

    @property
    def application_gate_progress_message(self) -> str | None: ...

    async def bootstrap(self, call: CallDescriptor) -> VoiceSessionConfig: ...

    async def observe_patient_transcript_delta(self, delta: str) -> str | None: ...

    async def complete_patient_turn(self, *, turn_epoch: int | None = None) -> str | None: ...

    async def arm_pending_confirmation(self, *, after_turn_epoch: int | None) -> None: ...

    async def handle_tool_intent(self, intent: ToolIntent) -> ToolResult: ...

    async def reconcile(self, operation_id: str) -> ToolResult: ...


class EventSink(Protocol):
    async def record(self, event_type: str, **metadata: Any) -> None: ...
