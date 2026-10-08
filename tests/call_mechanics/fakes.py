"""Strict test doubles shared by call-mechanics tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from clinic_agent.call_mechanics.models import (
    AudioChunk,
    CallDescriptor,
    SpeechClip,
    ToolIntent,
    ToolResult,
    ToolStatus,
    VoiceSessionConfig,
)


class FakeTelephony:
    def __init__(self) -> None:
        self.sent_audio: list[AudioChunk] = []
        self.marks: list[str] = []
        self.clear_count = 0
        self.transfers: list[str] = []
        self.close_reasons: list[str] = []

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_audio.append(chunk)

    async def clear_playback(self) -> None:
        self.clear_count += 1

    async def mark_playback(self, label: str) -> None:
        self.marks.append(label)

    async def transfer(self, destination: str) -> None:
        self.transfers.append(destination)

    async def close(self, reason: str) -> None:
        self.close_reasons.append(reason)


class FakeVoice:
    def __init__(
        self,
        connect_failures: int = 0,
        *,
        instruction_sequence: int | None = None,
    ) -> None:
        self.connect_failures = connect_failures
        self.connect_calls = 0
        self.configs: list[VoiceSessionConfig] = []
        self.pushed_audio: list[AudioChunk] = []
        self.cancel_count = 0
        self.truncations: list[tuple[str, int]] = []
        self.tool_results: list[ToolResult] = []
        self.cancelled_delegations: list[tuple[str, str]] = []
        self.commentary: list[str] = []
        self.thinking: list[str] = []
        self.instructions: list[str] = []
        self.instruction_sequence = instruction_sequence
        self.close_count = 0

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self.connect_failures:
            raise ConnectionError("synthetic voice connection failure")
        self.configs.append(config)

    async def push_audio(self, chunk: AudioChunk) -> None:
        self.pushed_audio.append(chunk)

    async def cancel_response(self) -> None:
        self.cancel_count += 1

    async def truncate_response(self, item_id: str, audio_end_ms: int) -> None:
        self.truncations.append((item_id, audio_end_ms))

    async def append_commentary(self, content: str) -> None:
        self.commentary.append(content)

    async def append_thinking(self, content: str) -> None:
        self.thinking.append(content)

    async def append_instructions(self, content: str) -> int | None:
        self.instructions.append(content)
        return self.instruction_sequence

    async def submit_tool_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)

    async def cancel_delegation(self, delegation_id: str, reason: str) -> None:
        self.cancelled_delegations.append((delegation_id, reason))

    async def close(self) -> None:
        self.close_count += 1


class FakeSpeechRenderer:
    def __init__(
        self,
        *,
        clip: SpeechClip | None = None,
        error: Exception | None = None,
    ) -> None:
        self.clip = clip or SpeechClip(b"\xff" * 800, 100)
        self.error = error
        self.texts: list[str] = []

    async def render(self, text: str) -> SpeechClip:
        self.texts.append(text)
        if self.error is not None:
            raise self.error
        return self.clip


class FakeControlPlane:
    def __init__(
        self,
        handler: Callable[[ToolIntent], Awaitable[ToolResult]] | None = None,
        transcript_directive: str | None = None,
        completed_turn_directive: str | None = None,
        application_owns_completed_turn: bool = False,
        application_gate_progress_message: str | None = None,
    ) -> None:
        self.bootstrap_calls: list[CallDescriptor] = []
        self.tool_calls: list[ToolIntent] = []
        self.reconcile_calls: list[str] = []
        self.transcript_deltas: list[str] = []
        self.completed_turn_count = 0
        self.completed_turn_epochs: list[int | None] = []
        self.armed_confirmation_epochs: list[int | None] = []
        self._handler = handler
        self._transcript_directive = transcript_directive
        self._completed_turn_directive = completed_turn_directive
        self._application_owns_completed_turn = application_owns_completed_turn
        self._application_gate_progress_message = application_gate_progress_message

    @property
    def application_owns_completed_turn(self) -> bool:
        return self._application_owns_completed_turn

    def begin_caller_turn(self, turn_epoch: int) -> None:
        pass

    @property
    def normal_conversation_active(self) -> bool:
        return not self._application_owns_completed_turn

    @property
    def application_gate_progress_message(self) -> str | None:
        return self._application_gate_progress_message

    async def bootstrap(self, call: CallDescriptor) -> VoiceSessionConfig:
        self.bootstrap_calls.append(call)
        return VoiceSessionConfig(instructions="synthetic clinic policy", context_reference=call.provider_call_id)

    async def handle_tool_intent(self, intent: ToolIntent) -> ToolResult:
        self.tool_calls.append(intent)
        if self._handler:
            return await self._handler(intent)
        return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED)

    async def observe_patient_transcript_delta(self, delta: str) -> str | None:
        self.transcript_deltas.append(delta)
        return self._transcript_directive

    async def complete_patient_turn(self, *, turn_epoch: int | None = None) -> str | None:
        self.completed_turn_count += 1
        self.completed_turn_epochs.append(turn_epoch)
        return self._completed_turn_directive

    async def arm_pending_confirmation(self, *, after_turn_epoch: int | None) -> None:
        self.armed_confirmation_epochs.append(after_turn_epoch)

    async def reconcile(self, operation_id: str) -> ToolResult:
        self.reconcile_calls.append(operation_id)
        return ToolResult(operation_id, ToolStatus.SUCCEEDED)


class ExplodingSink:
    async def record(self, event_type: str, **metadata: object) -> None:
        raise RuntimeError("telemetry unavailable")
