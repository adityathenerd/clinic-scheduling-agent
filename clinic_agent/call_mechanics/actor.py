"""One asynchronous actor owns all mutable state for one call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from .models import (
    AudioChunk,
    CallDescriptor,
    NormalizedEventKind,
    NORMAL_TURN_PROGRESS_TIMEOUT,
    OperationKind,
    SessionEvent,
    SessionState,
    ToolIntent,
    ToolResult,
    ToolStatus,
)
from .protocols import AgentControlPlane, EventSink, TelephonyAdapter, VoiceEngine
from .queue import BoundedAsyncQueue, QueueClosedError
from .worker_pool import AsyncWorkerPool


@dataclass(slots=True)
class _Envelope:
    event: SessionEvent
    completion: asyncio.Future[Any]


@dataclass(slots=True)
class _NormalTurnActivity:
    epoch: int
    token: str
    completed: bool = False
    progressed: bool = False
    failed: bool = False
    resumed: bool = False


class SessionClosedError(RuntimeError):
    pass


class CallSessionActor:
    """Serializes state transitions while delegating blocking I/O to workers."""

    _MEDIA_STATES = {SessionState.ACTIVE, SessionState.RECONCILIATION_REQUIRED}
    _TERMINAL_STATES = {
        SessionState.CLOSED,
        SessionState.FAILED,
        SessionState.RECONCILIATION_REQUIRED,
    }

    def __init__(
        self,
        descriptor: CallDescriptor,
        telephony: TelephonyAdapter,
        voice: VoiceEngine,
        control_plane: AgentControlPlane,
        event_sink: EventSink,
        worker_pool: AsyncWorkerPool,
        *,
        mailbox_capacity: int = 64,
        voice_connect_retries: int = 1,
        turn_completion_delay: float = 0.75,
        normal_turn_progress_timeout: float = NORMAL_TURN_PROGRESS_TIMEOUT,
    ) -> None:
        self.descriptor = descriptor
        self.telephony = telephony
        self.voice = voice
        self.control_plane = control_plane
        self.event_sink = event_sink
        self.worker_pool = worker_pool
        self.state = SessionState.NEW
        self._mailbox: BoundedAsyncQueue[_Envelope] = BoundedAsyncQueue(mailbox_capacity)
        self._voice_connect_retries = voice_connect_retries
        if turn_completion_delay < 0:
            raise ValueError("turn completion delay must be non-negative")
        self._turn_completion_delay = turn_completion_delay
        if normal_turn_progress_timeout <= 0:
            raise ValueError("normal turn progress timeout must be positive")
        self._normal_turn_progress_timeout = normal_turn_progress_timeout
        self._loop_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._done = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._caller_connected = True
        self._transports_closed = False
        self._assistant_item_id: str | None = None
        self._assistant_last_played_ms = 0
        self._assistant_transcript_fragments: list[str] = []
        self._caller_transcript_fragments: list[str] = []
        self._first_audio_emitted = False
        self._last_inbound_sequence: int | None = None
        self._operations: dict[str, dict[str, Any]] = {}
        self._active_delegation_id: str | None = None
        self._active_delegation_turn_epoch: int | None = None
        self._stale_delegations: set[str] = set()
        self._terminal_after_drain = SessionState.CLOSED
        self._caller_turn_epoch = 0
        self._turn_completion_task: asyncio.Task[None] | None = None
        self._turn_completion_inflight: int | None = None
        self._application_gate_epoch: int | None = None
        self._allow_application_gate_progress_audio = False
        self._pending_gate_progress_audio = False
        self._suppress_audio_before_sequence: int | None = None
        self._authoritative_audio_turn_epoch: int | None = None
        self._assistant_transcript_min_sequence: int | None = None
        self._output_takeover_pending = False
        self._output_takeover_operations: set[str] = set()
        self._authoritative_audio_pending = False
        self._caller_speaking = False
        self._progress_turn_epoch: int | None = None
        self._normal_turn_activity: _NormalTurnActivity | None = None
        self._normal_turn_watchdog_task: asyncio.Task[None] | None = None
        self._activity_sequence = 0
        self._exact_reply_takeover_active = False

    @property
    def pending_operation_count(self) -> int:
        return sum(1 for value in self._operations.values() if not value["done"])

    @property
    def done(self) -> asyncio.Event:
        return self._done

    async def start(self) -> "CallSessionActor":
        async with self._start_lock:
            if self.state is not SessionState.NEW:
                return self
            self.state = SessionState.STARTING
            self._loop_task = asyncio.create_task(self._run(), name=f"call:{self.descriptor.provider_call_id}")
            await self.dispatch(SessionEvent("bootstrap"))
            return self

    async def dispatch(self, event: SessionEvent) -> Any:
        if self._mailbox.closed:
            raise SessionClosedError(f"call session is terminal: {self.state.value}")
        loop = asyncio.get_running_loop()
        completion: asyncio.Future[Any] = loop.create_future()
        self._mailbox.put_nowait(_Envelope(event, completion))
        return await completion

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def close(self, reason: str = "requested") -> None:
        if not self._mailbox.closed:
            await self.dispatch(SessionEvent("telephony.stop", {"reason": reason}))
        await self._done.wait()

    async def _run(self) -> None:
        try:
            while True:
                envelope = await self._mailbox.get()
                if envelope is None:
                    break
                try:
                    result = await self._handle(envelope.event)
                except Exception as exc:
                    if not envelope.completion.done():
                        envelope.completion.set_exception(exc)
                else:
                    if not envelope.completion.done():
                        envelope.completion.set_result(result)
        finally:
            self._done.set()

    async def _handle(self, event: SessionEvent) -> Any:
        if event.kind == "bootstrap":
            return await self._bootstrap()
        if event.kind == "telephony.audio":
            if self._caller_connected and self.state in self._MEDIA_STATES:
                await self._forward_caller_audio(event.data["chunk"])
            return None
        if event.kind == "voice.audio":
            if self._caller_connected and self.state in self._MEDIA_STATES:
                await self._send_voice_audio(event)
            return None
        if event.kind == "playback.marked":
            self._update_playback_cursor(event)
            return None
        if event.kind == "caller.speech_started":
            self._cancel_normal_turn_watchdog()
            self._caller_turn_epoch += 1
            self._caller_speaking = True
            self.control_plane.begin_caller_turn(self._caller_turn_epoch)
            self._normal_turn_activity = None
            if self.control_plane.normal_conversation_active:
                self._activity_sequence += 1
                self._normal_turn_activity = _NormalTurnActivity(
                    self._caller_turn_epoch,
                    f"normal-turn-{self._caller_turn_epoch}-{self._activity_sequence}",
                )
            self._application_gate_epoch = (
                self._caller_turn_epoch
                if self.control_plane.application_owns_completed_turn
                else None
            )
            self._allow_application_gate_progress_audio = False
            self._pending_gate_progress_audio = False
            self._cancel_turn_completion()
            if self._application_gate_epoch is not None:
                await self._invalidate_active_delegation("application_gate_pending")
            await self._flush_assistant_transcript("caller_speech_started")
            return await self._interrupt_assistant()
        if event.kind == "caller.speech_stopped":
            self._caller_speaking = False
            if self.control_plane.application_owns_completed_turn:
                self._application_gate_epoch = self._caller_turn_epoch
                await self._invalidate_active_delegation("application_gate_pending")
                progress = self.control_plane.application_gate_progress_message
                if progress and self._progress_turn_epoch != self._caller_turn_epoch:
                    await self._append_control_directive(f"say_exactly: {progress}")
                    self._progress_turn_epoch = self._caller_turn_epoch
                    self._allow_application_gate_progress_audio = True
                    self._pending_gate_progress_audio = True
                    await self._record(
                        "control.application_gate_progress_appended",
                        turn_epoch=self._caller_turn_epoch,
                        message_characters=len(progress),
                        gate_kind=(
                            "identity" if "identity" in progress.casefold()
                            else "confirmation"
                        ),
                    )
            self._schedule_turn_completion(self._caller_turn_epoch)
            await self._record(
                "caller.turn_boundary_detected",
                turn_epoch=self._caller_turn_epoch,
            )
            return None
        if event.kind == "caller.turn_completed":
            if int(event.data.get("turn_epoch", -1)) != self._caller_turn_epoch:
                return None
            if (
                self.control_plane.application_owns_completed_turn
                and self._application_gate_epoch is None
            ):
                self._application_gate_epoch = self._caller_turn_epoch
                await self._invalidate_active_delegation("late_application_gate_pending")
            return await self._start_turn_completion_worker(self._caller_turn_epoch)
        if event.kind == "caller.turn_completion_processed":
            turn_epoch = int(event.data.get("turn_epoch", -1))
            try:
                if turn_epoch == self._caller_turn_epoch:
                    transcript = await self._flush_caller_transcript("turn_completed", turn_epoch)
                if self._turn_completion_inflight == turn_epoch:
                    self._turn_completion_inflight = None
                if turn_epoch != self._caller_turn_epoch or not self._caller_connected:
                    await self._record(
                        "control.completed_turn_stale",
                        turn_epoch=turn_epoch,
                    )
                    if self._caller_connected and not self._caller_speaking:
                        self._schedule_turn_completion(self._caller_turn_epoch)
                    return None
                error = event.data.get("error")
                if error is not None:
                    await self._record(
                        "control.completed_turn_failed",
                        turn_epoch=turn_epoch,
                        error_class=type(error).__name__,
                    )
                    if self.control_plane.normal_conversation_active:
                        await self._fail_normal_turn("completed_turn_failed")
                    return None
                directive = event.data.get("directive")
                if directive:
                    delivery_mode = await self._append_control_directive(str(directive))
                    await self._record(
                        "control.completed_turn_directive",
                        turn_epoch=turn_epoch,
                        directive_characters=len(str(directive)),
                        delivery_mode=delivery_mode,
                    )
                    await self._mark_normal_turn_progress("application_directive")
                elif self.control_plane.normal_conversation_active and transcript:
                    await self._finish_normal_turn(transcript)
                return None
            finally:
                if self._application_gate_epoch == turn_epoch:
                    self._application_gate_epoch = None
                    self._allow_application_gate_progress_audio = False
                    self._pending_gate_progress_audio = False
        if event.kind == "voice.input_transcript.delta":
            delta = str(event.data.get("delta", ""))
            if delta:
                self._caller_transcript_fragments.append(delta)
            directive = await self.control_plane.observe_patient_transcript_delta(delta)
            if directive:
                delivery_mode = await self._append_control_directive(directive)
                await self._record(
                    "control.directive_appended",
                    directive_characters=len(directive),
                    delivery_mode=delivery_mode,
                )
                await self._mark_normal_turn_progress("application_directive")
            elif (
                delta
                and not self._caller_speaking
                and self._turn_completion_inflight is None
                and self._application_gate_epoch is not None
            ):
                # Settle against the last delta, not just the audio/VAD boundary.
                self._schedule_turn_completion(self._caller_turn_epoch)
            return None
        if event.kind == "voice.output_transcript.delta":
            delta = str(event.data.get("delta", ""))
            transcript_sequence = event.data.get("audio_sequence")
            if (
                self._output_takeover_pending
                or self._output_takeover_operations
                or (
                    self._application_gate_epoch is not None
                    and not self._allow_application_gate_progress_audio
                )
                or (
                    self._assistant_transcript_min_sequence is not None
                    and isinstance(transcript_sequence, int)
                    and transcript_sequence < self._assistant_transcript_min_sequence
                )
            ):
                await self._record(
                    "assistant.transcript_suppressed",
                    audio_sequence=transcript_sequence,
                    authorized_sequence=self._assistant_transcript_min_sequence,
                    transcript_characters=len(delta),
                )
                return None
            if delta:
                self._assistant_transcript_fragments.append(delta)
                await self._mark_normal_turn_progress("assistant_transcript")
            return None
        if event.kind in {"caller.correction", "escalation.requested"}:
            await self._invalidate_active_delegation(event.kind)
            await self._record(event.kind)
            return None
        if event.kind == NormalizedEventKind.LIVE_SESSION_STARTED.value:
            await self._record(NormalizedEventKind.LIVE_SESSION_STARTED.value)
            return None
        if event.kind == NormalizedEventKind.DELEGATION_CREATED.value:
            return await self._handle_delegation_created(event)
        if event.kind == NormalizedEventKind.BACKEND_RESPONSE_STARTED.value:
            return await self._record_delegation_stage(event)
        if event.kind == NormalizedEventKind.BACKEND_TOOL_REQUESTED.value:
            return await self._start_tool_worker(event.data["intent"])
        if event.kind == NormalizedEventKind.BACKEND_TOOL_COMPLETED.value:
            return await self._record_delegation_stage(event)
        if event.kind == NormalizedEventKind.DELEGATION_COMPLETED.value:
            return await self._handle_delegation_completed(event)
        if event.kind == NormalizedEventKind.DELEGATION_CANCELLED.value:
            return await self._handle_delegation_cancelled(event)
        if event.kind == NormalizedEventKind.LIVE_COMMENTARY_APPENDED.value:
            await self._record(
                NormalizedEventKind.LIVE_COMMENTARY_APPENDED.value,
                delegation_id=event.data.get("delegation_id"),
                character_count=int(event.data.get("character_count", 0)),
            )
            return None
        if event.kind == NormalizedEventKind.LIVE_SESSION_CLOSED.value:
            await self._record(
                NormalizedEventKind.LIVE_SESSION_CLOSED.value,
                reason=str(event.data.get("reason", "normal")),
            )
            return None
        if event.kind == "tool.requested":
            return await self._start_tool_worker(event.data["intent"])
        if event.kind == "worker.completed":
            return await self._complete_tool_worker(event)
        if event.kind == "telephony.stop":
            return await self._handle_stop(str(event.data.get("reason", "remote_stop")))
        if event.kind == "voice.error":
            await self._mark_normal_turn_progress("voice_error")
            return await self._handle_voice_failure(str(event.data.get("reason", "voice_error")))
        if event.kind == "control.normal_turn_timeout":
            activity = self._normal_turn_activity
            if (
                activity is not None
                and activity.token == event.data.get("activity_token")
                and activity.epoch == event.data.get("turn_epoch")
                and not activity.progressed
                and not activity.failed
                and self._caller_connected
                and not self._caller_speaking
                and self.control_plane.normal_conversation_active
            ):
                # Never replay work that has already entered the backend.
                if self._normal_turn_has_inflight_work():
                    await self._mark_normal_turn_progress("in_flight_backend")
                else:
                    await self._fail_normal_turn("downstream_progress_timeout")
            return None
        if event.kind == "control.normal_turn_resume":
            await self._resume_normal_turn(event)
            return None

        if event.kind == "provider.unknown_event":
            await self._record(
                "provider.unknown_event",
                provider_type=str(event.data.get("provider_type", "unknown")),
            )
            return None
        await self._record("provider.unknown_event", provider_type=event.kind)
        return None

    async def _bootstrap(self) -> None:
        config = await self.control_plane.bootstrap(self.descriptor)
        last_error: Exception | None = None
        for attempt in range(self._voice_connect_retries + 1):
            try:
                await self.voice.connect(config)
                last_error = None
                break
            except Exception as exc:  # provider adapters normalize their own errors
                last_error = exc
                await self._record("voice.connect_failed", attempt=attempt + 1)
        if last_error is not None:
            if self.descriptor.fallback_destination:
                await self.telephony.transfer(self.descriptor.fallback_destination)
            self.state = SessionState.FAILED
            await self._close_transports_once("voice_connect_failed")
            self._mailbox.close()
            raise last_error

        self.state = SessionState.ACTIVE
        await self._record(
            "call.started",
            direction=self.descriptor.direction.value,
        )
        await self._record("voice.connected")

    async def _send_voice_audio(self, event: SessionEvent) -> None:
        chunk = event.data["chunk"]
        if not isinstance(chunk, AudioChunk):
            raise TypeError("voice.audio requires an AudioChunk")
        sequence_cutoff = self._suppress_audio_before_sequence
        gate_blocks_audio = (
            self._output_takeover_pending
            or bool(self._output_takeover_operations)
            or (
                self._application_gate_epoch is not None
                and not self._allow_application_gate_progress_audio
            )
        )
        if gate_blocks_audio or (
            sequence_cutoff is not None and chunk.sequence < sequence_cutoff
        ):
            await self._record(
                "assistant.audio_suppressed",
                reason=(
                    "application_gate_pending"
                    if gate_blocks_audio
                    else "pre_instruction_timeline"
                ),
                turn_epoch=self._application_gate_epoch,
                audio_bytes=len(chunk.payload),
                audio_sequence=chunk.sequence,
                authorized_sequence=sequence_cutoff,
            )
            return
        if (
            self._application_gate_epoch is not None
            and self._allow_application_gate_progress_audio
            and self._pending_gate_progress_audio
        ):
            self._pending_gate_progress_audio = False
            await self._record(
                "assistant.gate_progress_audio_started",
                turn_epoch=self._application_gate_epoch,
                audio_sequence=chunk.sequence,
            )
        if sequence_cutoff is not None and self._authoritative_audio_pending:
            await self._record(
                "assistant.authoritative_audio_started",
                turn_epoch=self._authoritative_audio_turn_epoch,
                audio_sequence=chunk.sequence,
                authorized_sequence=sequence_cutoff,
            )
            self._authoritative_audio_pending = False
            self._authoritative_audio_turn_epoch = None
        item_id = str(event.data["item_id"])
        audio_end_ms = int(event.data["audio_end_ms"])
        await self.telephony.send_audio(chunk)
        await self._mark_normal_turn_progress("assistant_audio")
        await self.telephony.mark_playback(f"{item_id}:{audio_end_ms}")
        self._assistant_item_id = item_id
        if not self._first_audio_emitted:
            self._first_audio_emitted = True
            await self._record("assistant.first_audio")

    async def _forward_caller_audio(self, chunk: AudioChunk) -> None:
        if not isinstance(chunk, AudioChunk):
            raise TypeError("telephony.audio requires an AudioChunk")
        if self._last_inbound_sequence is not None:
            if chunk.sequence <= self._last_inbound_sequence:
                await self._record(
                    "media.duplicate_or_out_of_order",
                    sequence=chunk.sequence,
                    last_sequence=self._last_inbound_sequence,
                )
                return
            if chunk.sequence > self._last_inbound_sequence + 1:
                await self._record(
                    "media.sequence_gap",
                    expected_sequence=self._last_inbound_sequence + 1,
                    actual_sequence=chunk.sequence,
                )
        self._last_inbound_sequence = chunk.sequence
        await self.voice.push_audio(chunk)

    def _update_playback_cursor(self, event: SessionEvent) -> None:
        item_id = str(event.data["item_id"])
        audio_end_ms = int(event.data["audio_end_ms"])
        if item_id == self._assistant_item_id:
            self._assistant_last_played_ms = max(self._assistant_last_played_ms, audio_end_ms)

    async def _interrupt_assistant(self) -> None:
        if not self._assistant_item_id or not self._caller_connected:
            return
        item_id = self._assistant_item_id
        played_ms = self._assistant_last_played_ms
        await self.telephony.clear_playback()
        await self.voice.cancel_response()
        await self.voice.truncate_response(item_id, played_ms)
        await self._record("assistant.interrupted", item_id=item_id, audio_end_ms=played_ms)
        self._assistant_item_id = None
        self._assistant_last_played_ms = 0

    async def _handle_delegation_created(self, event: SessionEvent) -> None:
        delegation_id = str(event.data["delegation_id"])
        if self._active_delegation_id and self._active_delegation_id != delegation_id:
            await self._invalidate_active_delegation("superseded")
        self._active_delegation_id = delegation_id
        self._active_delegation_turn_epoch = self._caller_turn_epoch
        self._stale_delegations.discard(delegation_id)
        await self._record(
            NormalizedEventKind.DELEGATION_CREATED.value,
            delegation_id=delegation_id,
            turn_epoch=self._active_delegation_turn_epoch,
        )
        if self._normal_turn_activity is not None and self._normal_turn_activity.failed:
            await self._invalidate_active_delegation("normal_turn_liveness_failed")
        elif self._application_gate_epoch is not None:
            await self._invalidate_active_delegation("application_gate_pending")
        else:
            await self._mark_normal_turn_progress("delegation_created")

    async def _record_delegation_stage(self, event: SessionEvent) -> bool:
        delegation_id = str(event.data["delegation_id"])
        if not self._is_current_delegation(delegation_id):
            await self._record(
                NormalizedEventKind.DELEGATION_STALE.value,
                delegation_id=delegation_id,
                stage=event.kind,
            )
            return False
        await self._record(
            event.kind, delegation_id=delegation_id,
            turn_epoch=self._active_delegation_turn_epoch,
        )
        if self._active_delegation_turn_epoch == self._caller_turn_epoch:
            await self._mark_normal_turn_progress(event.kind)
        return True

    async def _handle_delegation_completed(self, event: SessionEvent) -> bool:
        delegation_id = str(event.data["delegation_id"])
        if not self._is_current_delegation(delegation_id):
            await self._record(
                NormalizedEventKind.DELEGATION_STALE.value,
                delegation_id=delegation_id,
                stage=NormalizedEventKind.DELEGATION_COMPLETED.value,
            )
            return False
        await self._record(
            NormalizedEventKind.DELEGATION_COMPLETED.value,
            delegation_id=delegation_id,
        )
        self._active_delegation_id = None
        self._active_delegation_turn_epoch = None
        return True

    async def _handle_delegation_cancelled(self, event: SessionEvent) -> None:
        delegation_id = str(event.data["delegation_id"])
        self._stale_delegations.add(delegation_id)
        if self._active_delegation_id == delegation_id:
            self._active_delegation_id = None
            self._active_delegation_turn_epoch = None
        await self._record(
            NormalizedEventKind.DELEGATION_CANCELLED.value,
            delegation_id=delegation_id,
            reason=str(event.data.get("reason", "backend_cancelled")),
        )

    async def _invalidate_active_delegation(self, reason: str) -> None:
        delegation_id = self._active_delegation_id
        if delegation_id is None:
            return
        self._active_delegation_id = None
        self._active_delegation_turn_epoch = None
        self._stale_delegations.add(delegation_id)
        for operation in self._operations.values():
            intent: ToolIntent = operation["intent"]
            if intent.delegation_id == delegation_id and not operation["done"]:
                operation["stale"] = True
        await self.voice.cancel_delegation(delegation_id, reason)
        await self._record(
            NormalizedEventKind.DELEGATION_CANCELLED.value,
            delegation_id=delegation_id,
            reason=reason,
        )
        await self._record(
            NormalizedEventKind.DELEGATION_STALE.value,
            delegation_id=delegation_id,
            stage="invalidated",
        )

    def _is_current_delegation(self, delegation_id: str) -> bool:
        return (
            delegation_id == self._active_delegation_id
            and delegation_id not in self._stale_delegations
        )

    async def _start_tool_worker(self, intent: ToolIntent) -> bool:
        if intent.delegation_id and not self._is_current_delegation(intent.delegation_id):
            await self._record(
                NormalizedEventKind.DELEGATION_STALE.value,
                delegation_id=intent.delegation_id,
                stage=NormalizedEventKind.BACKEND_TOOL_REQUESTED.value,
            )
            return False
        if intent.state_revision is None:
            origin_epoch = (
                self._active_delegation_turn_epoch
                if intent.delegation_id else self._caller_turn_epoch
            )
            intent = replace(intent, state_revision=origin_epoch)
        existing = self._operations.get(intent.operation_id)
        if existing is not None:
            await self._record("tool.duplicate_ignored", operation_id=intent.operation_id)
            return False
        if intent.state_revision == self._caller_turn_epoch:
            await self._mark_normal_turn_progress("tool_requested")

        self._operations[intent.operation_id] = {
            "intent": intent,
            "done": False,
            "result": None,
            "stale": False,
        }
        if intent.operation_kind is OperationKind.WRITE:
            # A speculative write prepares the exact proposal. Take ownership
            # before the worker or its result can trigger a Live preamble.
            self._output_takeover_operations.add(intent.operation_id)
            await self.telephony.clear_playback()
            await self.voice.cancel_response()
            await self._flush_assistant_transcript("application_write_started")
        self._idle.clear()

        task = self.worker_pool.submit(lambda: self.control_plane.handle_tool_intent(intent))

        def worker_finished(completed: asyncio.Task[ToolResult]) -> None:
            try:
                result = completed.result()
                error: Exception | None = None
            except Exception as exc:  # converted to a typed result inside the actor
                result = None
                error = exc
            async def deliver_result() -> None:
                try:
                    await self.dispatch(
                        SessionEvent(
                            "worker.completed",
                            {"operation_id": intent.operation_id, "result": result, "error": error},
                        )
                    )
                except (RuntimeError, SessionClosedError, QueueClosedError):
                    # The actor is already terminal; no mutable state remains to update.
                    return

            try:
                asyncio.create_task(deliver_result())
            except RuntimeError:
                return

        task.add_done_callback(worker_finished)
        await self._record(
            "tool.requested",
            operation_id=intent.operation_id,
            tool_name=intent.tool_name,
            arguments=dict(intent.arguments),
            turn_epoch=intent.state_revision,
        )
        return True

    async def _complete_tool_worker(self, event: SessionEvent) -> None:
        operation_id = str(event.data["operation_id"])
        operation = self._operations[operation_id]
        intent: ToolIntent = operation["intent"]
        result: ToolResult | None = event.data.get("result")
        error: Exception | None = event.data.get("error")

        if error is not None:
            result = ToolResult(
                operation_id=operation_id,
                status=(ToolStatus.UNKNOWN if intent.operation_kind is OperationKind.WRITE else ToolStatus.FAILED),
                error_class=type(error).__name__,
            )
        if result is None:
            raise RuntimeError("worker completed without a result")
        if result.operation_id != operation_id:
            await self._record(
                "tool.result_mismatch",
                expected_operation_id=operation_id,
                actual_operation_id=result.operation_id,
            )
            result = ToolResult(
                operation_id=operation_id,
                status=(ToolStatus.UNKNOWN if intent.operation_kind is OperationKind.WRITE else ToolStatus.FAILED),
                error_class="OperationIdMismatch",
            )

        operation["done"] = True
        self._output_takeover_operations.discard(operation_id)
        operation["result"] = result
        is_stale = bool(operation.get("stale")) or (
            intent.delegation_id is not None
            and intent.delegation_id in self._stale_delegations
        )
        if result.status is ToolStatus.UNKNOWN and intent.operation_kind is OperationKind.WRITE:
            self.state = SessionState.RECONCILIATION_REQUIRED
            await self._record("tool.outcome_unknown", operation_id=operation_id, tool_name=intent.tool_name)
        else:
            await self._record(
                "tool.completed",
                operation_id=operation_id,
                tool_name=intent.tool_name,
                status=result.status.value,
                turn_epoch=intent.state_revision,
            )

        if intent.delegation_id:
            await self._record(
                NormalizedEventKind.BACKEND_TOOL_COMPLETED.value,
                delegation_id=intent.delegation_id,
                operation_id=operation_id,
                status=result.status.value,
                stale=is_stale,
            )
        if is_stale:
            await self._record(
                NormalizedEventKind.DELEGATION_STALE.value,
                delegation_id=intent.delegation_id,
                stage=NormalizedEventKind.BACKEND_TOOL_COMPLETED.value,
            )
        elif self._caller_connected:
            application_directive = result.payload.get("application_directive")
            domain_result = result.payload.get("result")
            is_confirmation_proposal = (
                isinstance(domain_result, dict)
                and domain_result.get("status") == "confirmation_required"
            )
            if (
                isinstance(application_directive, str)
                and application_directive.strip()
                and (
                    is_confirmation_proposal
                    or application_directive.casefold().startswith("say_exactly:")
                )
            ):
                # A caller turn that began while the proposal was being prepared
                # cannot confirm wording that had not yet been delivered.
                proposal_epoch = max(
                    intent.state_revision or 0, self._caller_turn_epoch
                )
                self._application_gate_epoch = proposal_epoch
                # Return the result only after taking speech ownership. Do not
                # let a delegated response add wording to the exact proposal.
                await self.voice.submit_tool_result(result)
                await self._invalidate_active_delegation("application_proposal_owned")
                delivery_mode = await self._append_control_directive(
                    application_directive
                )
                if is_confirmation_proposal:
                    await self.control_plane.arm_pending_confirmation(
                        after_turn_epoch=proposal_epoch
                    )
                await self._record(
                    (
                        "confirmation.proposal_directive_appended"
                        if is_confirmation_proposal
                        else "control.directive_appended"
                    ),
                    operation_id=operation_id,
                    turn_epoch=proposal_epoch,
                    delivery_mode=delivery_mode,
                    directive_digest=result.payload.get(
                        "application_directive_digest"
                    ),
                )
                self._application_gate_epoch = None
            else:
                await self.voice.submit_tool_result(result)
        if self.pending_operation_count == 0:
            self._idle.set()
            if not self._caller_connected:
                terminal = (
                    SessionState.RECONCILIATION_REQUIRED
                    if self.state is SessionState.RECONCILIATION_REQUIRED
                    else self._terminal_after_drain
                )
                await self._finalize(terminal)

    async def _handle_stop(self, reason: str) -> None:
        self._cancel_normal_turn_watchdog()
        self._cancel_turn_completion()
        await self._flush_caller_transcript("call_stopped", self._caller_turn_epoch)
        await self._flush_assistant_transcript("call_stopped")
        self._caller_connected = False
        self._terminal_after_drain = SessionState.CLOSED
        await self._record("call.remote_stop", reason=reason)
        await self._close_transports_once(reason)
        if self.pending_operation_count:
            if self.state is not SessionState.RECONCILIATION_REQUIRED:
                self.state = SessionState.DRAINING
            return
        terminal = (
            SessionState.RECONCILIATION_REQUIRED
            if self.state is SessionState.RECONCILIATION_REQUIRED
            else SessionState.CLOSED
        )
        await self._finalize(terminal)

    async def _handle_voice_failure(self, reason: str) -> None:
        self._cancel_normal_turn_watchdog()
        self._cancel_turn_completion()
        await self._flush_assistant_transcript("voice_failed")
        await self._record("voice.failed_mid_call", reason=reason)
        self._caller_connected = False
        self._terminal_after_drain = SessionState.FAILED
        if self.descriptor.fallback_destination:
            await self.telephony.transfer(self.descriptor.fallback_destination)
        await self._close_transports_once(reason)
        if self.pending_operation_count:
            self.state = SessionState.DRAINING
            return
        await self._finalize(SessionState.FAILED)

    async def _finalize(self, terminal: SessionState) -> None:
        self._cancel_normal_turn_watchdog()
        self._cancel_turn_completion()
        self.state = terminal
        await self._close_transports_once(terminal.value)
        await self._record("call.ended", outcome=terminal.value)
        self._mailbox.close()

    def _schedule_turn_completion(self, turn_epoch: int) -> None:
        self._cancel_turn_completion()

        async def complete_after_transcript_settles() -> None:
            try:
                await asyncio.sleep(self._turn_completion_delay)
                await self.dispatch(
                    SessionEvent("caller.turn_completed", {"turn_epoch": turn_epoch})
                )
            except (asyncio.CancelledError, SessionClosedError, QueueClosedError):
                return

        self._turn_completion_task = asyncio.create_task(
            complete_after_transcript_settles(),
            name=f"turn-boundary:{self.descriptor.provider_call_id}:{turn_epoch}",
        )

    async def _start_turn_completion_worker(self, turn_epoch: int) -> bool:
        if self._turn_completion_inflight is not None:
            await self._record(
                "control.completed_turn_duplicate_ignored",
                turn_epoch=turn_epoch,
            )
            return False
        self._turn_completion_inflight = turn_epoch
        task = self.worker_pool.submit(
            lambda: self.control_plane.complete_patient_turn(turn_epoch=turn_epoch)
        )

        def completed(finished: asyncio.Task[str | None]) -> None:
            try:
                directive = finished.result()
                error: Exception | None = None
            except Exception as exc:
                directive = None
                error = exc

            async def deliver() -> None:
                try:
                    await self.dispatch(
                        SessionEvent(
                            "caller.turn_completion_processed",
                            {
                                "turn_epoch": turn_epoch,
                                "directive": directive,
                                "error": error,
                            },
                        )
                    )
                except (SessionClosedError, QueueClosedError):
                    return
                except Exception as exc:
                    await self._record(
                        "control.completed_turn_delivery_failed",
                        turn_epoch=turn_epoch,
                        error_class=type(exc).__name__,
                    )
                    try:
                        await self.dispatch(
                            SessionEvent(
                                "voice.error",
                                {
                                    "reason": (
                                        "authoritative_directive_delivery_"
                                        f"{type(exc).__name__}"
                                    )
                                },
                            )
                        )
                    except (RuntimeError, SessionClosedError, QueueClosedError):
                        return

            try:
                asyncio.create_task(deliver())
            except RuntimeError:
                return

        task.add_done_callback(completed)
        await self._record(
            "control.completed_turn_started",
            turn_epoch=turn_epoch,
        )
        return True

    def _cancel_turn_completion(self) -> None:
        task = self._turn_completion_task
        self._turn_completion_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _finish_normal_turn(self, transcript: str) -> None:
        activity = self._normal_turn_activity
        if activity is None or activity.epoch != self._caller_turn_epoch:
            return
        if activity.completed:
            return
        activity.completed = True
        await self._record(
            "control.normal_turn_completed",
            turn_epoch=activity.epoch,
            activity_token=activity.token,
            progress_seen=activity.progressed,
            transcript_characters=len(transcript),
        )
        if activity.progressed:
            return
        if self._normal_turn_has_inflight_work():
            await self._mark_normal_turn_progress("in_flight_backend")
            return
        await self._start_normal_turn_watchdog(activity)
        if self._exact_reply_takeover_active and not activity.resumed:
            async def deliver_resume() -> None:
                try:
                    await self.dispatch(SessionEvent(
                        "control.normal_turn_resume",
                        {"turn_epoch": activity.epoch, "activity_token": activity.token},
                    ))
                except (SessionClosedError, QueueClosedError):
                    return
            # Let downstream progress already in the mailbox win before deciding
            # whether a resume is necessary. This is never a tool/response retry.
            asyncio.create_task(deliver_resume())

    async def _resume_normal_turn(self, event: SessionEvent) -> None:
        activity = self._normal_turn_activity
        if (
            activity is None
            or activity.token != event.data.get("activity_token")
            or activity.epoch != self._caller_turn_epoch
            or activity.progressed
            or activity.failed
            or self._caller_speaking
            or not self._caller_connected
            or not self.control_plane.normal_conversation_active
        ):
            return
        if self._normal_turn_has_inflight_work():
            await self._mark_normal_turn_progress("in_flight_backend")
            return
        if self.pending_operation_count or self._active_delegation_id:
            # Earlier work is not progress for this turn, but don't replay it.
            # Keep this turn's watchdog armed so silence is still bounded.
            return
        if self._exact_reply_takeover_active and not activity.resumed:
            # instructions.append persists in the session. End the preceding
            # one-response restriction once, after a complete ordinary turn.
            # Do not replay the request if Live has already started work.
            activity.resumed = True
            self._output_takeover_pending = True
            try:
                cutoff = await self.voice.append_instructions(
                    "Authoritative application instruction: the preceding exact "
                    "reply has been delivered and its one-response wording "
                    "restriction has ended. Resume normal conversation for this "
                    "latest completed caller turn already in the conversation. "
                    "Respond naturally and delegate to "
                    "the guarded backend when tools are needed. Do not repeat "
                    "the preceding reply. Identity and scheduling guards remain "
                    "authoritative; this instruction grants no mutation consent."
                )
                if cutoff is not None:
                    self._suppress_audio_before_sequence = cutoff
                    self._assistant_transcript_min_sequence = cutoff
                self._exact_reply_takeover_active = False
                self._output_takeover_pending = False
                await self._record(
                    "control.normal_turn_resumed",
                    turn_epoch=activity.epoch,
                    activity_token=activity.token,
                    delivery_mode="instructions",
                )
            except Exception as exc:
                await self._fail_normal_turn(
                    "resume_delivery_failed", error_class=type(exc).__name__
                )

    def _normal_turn_has_inflight_work(self) -> bool:
        return (
            self._active_delegation_id is not None
            and self._active_delegation_turn_epoch == self._caller_turn_epoch
        ) or any(
            not operation["done"]
            and not operation.get("stale")
            and operation["intent"].state_revision == self._caller_turn_epoch
            for operation in self._operations.values()
        )

    async def _start_normal_turn_watchdog(self, activity: _NormalTurnActivity) -> None:
        self._cancel_normal_turn_watchdog()

        async def watch() -> None:
            try:
                await asyncio.sleep(self._normal_turn_progress_timeout)
                await self.dispatch(SessionEvent(
                    "control.normal_turn_timeout",
                    {"turn_epoch": activity.epoch, "activity_token": activity.token},
                ))
            except (asyncio.CancelledError, SessionClosedError, QueueClosedError):
                return

        self._normal_turn_watchdog_task = asyncio.create_task(
            watch(), name=f"normal-turn-watchdog:{self.descriptor.provider_call_id}:{activity.token}"
        )
        await self._record(
            "control.normal_turn_watchdog_started",
            turn_epoch=activity.epoch,
            activity_token=activity.token,
            timeout_seconds=self._normal_turn_progress_timeout,
        )

    def _cancel_normal_turn_watchdog(self) -> None:
        task = self._normal_turn_watchdog_task
        self._normal_turn_watchdog_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _mark_normal_turn_progress(self, kind: str) -> None:
        activity = self._normal_turn_activity
        if (
            activity is None
            or activity.epoch != self._caller_turn_epoch
            or activity.progressed
            or activity.failed
            or self._caller_speaking
        ):
            return
        activity.progressed = True
        self._cancel_normal_turn_watchdog()
        await self._record(
            "control.normal_turn_progress",
            turn_epoch=activity.epoch,
            activity_token=activity.token,
            progress_kind=kind,
        )

    async def _fail_normal_turn(self, reason: str, *, error_class: str | None = None) -> None:
        activity = self._normal_turn_activity
        if activity is None or activity.failed or activity.progressed:
            return
        activity.failed = True
        self._cancel_normal_turn_watchdog()
        await self._record(
            "control.normal_turn_liveness_failed",
            turn_epoch=activity.epoch,
            activity_token=activity.token,
            reason=reason,
            error_class=error_class,
            timeout_seconds=self._normal_turn_progress_timeout,
        )
        if reason == "resume_delivery_failed":
            await self._handle_voice_failure("normal_turn_resume_delivery_failed")
            return
        try:
            delivery = await self._append_control_directive(
                "say_exactly: I didn't get a response to that request. "
                "I have not made a scheduling change. Please repeat your request "
                "or ask for the front desk."
            )
            await self._record(
                "control.normal_turn_recovery_appended",
                turn_epoch=activity.epoch,
                activity_token=activity.token,
                delivery_mode=delivery,
            )
        except Exception as exc:
            await self._record(
                "control.normal_turn_recovery_failed",
                turn_epoch=activity.epoch,
                activity_token=activity.token,
                error_class=type(exc).__name__,
            )
            await self._handle_voice_failure("normal_turn_recovery_delivery_failed")

    async def _flush_assistant_transcript(self, reason: str) -> None:
        if not self._assistant_transcript_fragments:
            return
        transcript = "".join(self._assistant_transcript_fragments).strip()
        self._assistant_transcript_fragments.clear()
        if not transcript:
            return
        await self._record(
            "assistant.turn",
            transcript=transcript,
            transcript_characters=len(transcript),
            reason=reason,
        )

    async def _flush_caller_transcript(self, reason: str, turn_epoch: int) -> str | None:
        if not self._caller_transcript_fragments:
            return
        transcript = "".join(self._caller_transcript_fragments).strip()
        self._caller_transcript_fragments.clear()
        if not transcript:
            return
        await self._record(
            "caller.turn",
            transcript=transcript,
            transcript_characters=len(transcript),
            reason=reason,
            turn_epoch=turn_epoch,
        )
        return transcript

    async def _append_control_directive(self, directive: str) -> str:
        self._output_takeover_pending = True
        self._allow_application_gate_progress_audio = False
        await self._invalidate_active_delegation("application_reply_owned")
        await self.telephony.clear_playback()
        await self.voice.cancel_response()
        # Progress and final replies are separate, auditable spoken turns.
        await self._flush_assistant_transcript("application_reply_boundary")
        marker = "say_exactly:"
        if directive.casefold().startswith(marker):
            spoken_text = directive[len(marker):].strip()
            if not spoken_text:
                raise ValueError("say_exactly directive requires spoken text")
            self._exact_reply_takeover_active = True
            authorized_sequence = await self.voice.append_instructions(
                "Authoritative application instruction: this update supersedes "
                "any older workflow-state assumption or planned response. "
                "For your next response, say exactly the following text and nothing "
                "else. This wording restriction applies to this one response; "
                "then await the next caller turn. Do not add a preamble or "
                f"postscript:\n{spoken_text}"
            )
            if authorized_sequence is not None:
                self._suppress_audio_before_sequence = authorized_sequence
                self._assistant_transcript_min_sequence = authorized_sequence
                self._authoritative_audio_turn_epoch = self._application_gate_epoch
                self._authoritative_audio_pending = True
            self._output_takeover_pending = False
            return "instructions"
        authorized_sequence = await self.voice.append_instructions(
            "Authoritative application instruction: this update supersedes any "
            "older workflow-state assumption. Follow it before taking any other "
            f"action or speaking:\n{directive}"
        )
        if authorized_sequence is not None:
            self._suppress_audio_before_sequence = authorized_sequence
            self._assistant_transcript_min_sequence = authorized_sequence
            self._authoritative_audio_turn_epoch = self._application_gate_epoch
            self._authoritative_audio_pending = True
        self._output_takeover_pending = False
        return "instructions"

    async def _close_transports_once(self, reason: str) -> None:
        if self._transports_closed:
            return
        self._transports_closed = True
        await asyncio.gather(
            self.telephony.close(reason),
            self.voice.close(),
            return_exceptions=True,
        )

    async def _record(self, event_type: str, **metadata: Any) -> None:
        try:
            await self.event_sink.record(
                event_type,
                provider_call_id=self.descriptor.provider_call_id,
                session_state=self.state.value,
                **metadata,
            )
        except Exception:
            # Observability must not become a call-path dependency.
            return
