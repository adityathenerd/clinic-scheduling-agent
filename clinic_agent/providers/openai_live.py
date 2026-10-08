"""OpenAI GPT-Live implementation of the provider-neutral VoiceEngine facade.

The adapter deliberately sends only commands supported by the Live API.  In
particular, Live has no response-cancel or conversation-item-truncate command;
those facade methods are safe no-ops and interruption is enforced by clearing
the telephony playback buffer.  Delegation invalidation is likewise owned by
the application actor: late Responses events are ignored after cancellation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import Any, Protocol, cast
from uuid import uuid4

from openai import AsyncOpenAI

from clinic_agent.call_mechanics.models import (
    AudioChunk,
    NormalizedEventKind,
    OperationKind,
    SessionEvent,
    ToolIntent,
    ToolResult,
    VoiceArchitecture,
    VoiceSessionConfig,
)


EventCallback = Callable[[SessionEvent], Awaitable[None]]
ConnectionFactory = Callable[[], Any]

_WRITE_TOOLS = frozenset(
    {"create_appointment", "edit_appointment", "delete_appointment"}
)

# Documented Responses streaming lifecycle events that are informative for a UI
# but do not change application state. Keeping them out of the actor mailbox
# prevents hundreds of false "unknown" anomalies per voice turn.
_PASSIVE_RESPONSE_EVENTS = frozenset(
    {
        "response.queued",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.delta",
        "response.output_text.done",
        "response.refusal.delta",
        "response.refusal.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
    }
)

_PASSIVE_SESSION_EVENTS = frozenset(
    {
        "session.commentary.appended",
        "session.thinking.appended",
        "session.usage.updated",
    }
)


class _LiveConnection(Protocol):
    async def send(self, event: Mapping[str, Any]) -> None: ...

    async def recv(self) -> Any: ...

    async def close(self, *, code: int = 1000, reason: str = "") -> None: ...


class GPTLiveVoiceEngine:
    """Translate GPT-Live wire events into stable application session events."""

    def __init__(
        self,
        on_event: EventCallback,
        *,
        client: AsyncOpenAI | None = None,
        connection_factory: ConnectionFactory | None = None,
        startup_timeout: float = 10.0,
        close_timeout: float = 1.0,
        instruction_ack_timeout: float = 3.0,
    ) -> None:
        if client is not None and connection_factory is not None:
            raise ValueError("provide either client or connection_factory, not both")
        if startup_timeout <= 0 or close_timeout < 0 or instruction_ack_timeout <= 0:
            raise ValueError(
                "timeouts must be positive (close_timeout may be zero)"
            )
        self._on_event = on_event
        self._client = client
        self._connection_factory = connection_factory
        self._startup_timeout = startup_timeout
        self._close_timeout = close_timeout
        self._instruction_ack_timeout = instruction_ack_timeout
        self._connection: _LiveConnection | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._event_queue: asyncio.Queue[SessionEvent] | None = None
        self._closing = False
        self._closed_event = asyncio.Event()
        self._audio_sequence = 0
        self._output_audio_bytes = 0
        self._pending_operations: dict[str, str | None] = {}
        self._cancelled_delegations: set[str] = set()
        self._active_response_ids: dict[str, str] = {}
        self._tool_response_ids: set[tuple[str, str]] = set()
        self._pending_instruction_acks: dict[str, asyncio.Future[int]] = {}

    async def connect(self, config: VoiceSessionConfig) -> None:
        """Open a primary WebSocket, start a session, and await acceptance."""

        if self._connection is not None:
            raise RuntimeError("GPT-Live voice engine is already connected")
        if config.architecture is not VoiceArchitecture.LIVE_DELEGATED:
            raise ValueError("GPTLiveVoiceEngine requires live_delegated architecture")

        connection = await self._open_connection()
        self._connection = connection
        session = self._build_session_config(config)
        try:
            await connection.send({"type": "session.start", "session": session})
            initial_events: list[Any] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._startup_timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("GPT-Live did not emit session.started")
                event = await asyncio.wait_for(connection.recv(), timeout=remaining)
                event_data = _as_mapping(event)
                event_type = str(event_data.get("type", ""))
                if event_type == "error":
                    raise RuntimeError(_error_reason(event_data))
                initial_events.append(event)
                if event_type == "session.started":
                    break
            if config.initial_commentary:
                await connection.send(
                    {
                        "type": "session.commentary.append",
                        "content": config.initial_commentary,
                        "delegation_id": None,
                    }
                )
        except BaseException:
            self._connection = None
            await connection.close(reason="startup_failed")
            raise

        # Socket reads and application callbacks must remain independent. The
        # reader owns protocol progress (including instruction acknowledgements),
        # while the delivery loop serializes normalized events for the actor.
        # Coupling them creates a circular wait when the actor awaits an ack while
        # an earlier audio callback is waiting in the actor mailbox.
        self._event_queue = asyncio.Queue()
        self._delivery_task = asyncio.create_task(
            self._delivery_loop(), name="gpt-live-delivery"
        )
        self._reader_task = asyncio.create_task(
            self._receive_loop(initial_events), name="gpt-live-events"
        )

    async def push_audio(self, chunk: AudioChunk) -> None:
        connection = self._require_connection()
        await connection.send(
            {
                "type": "session.input_audio.append",
                "audio": base64.b64encode(chunk.payload).decode("ascii"),
            }
        )

    async def cancel_response(self) -> None:
        """No-op: GPT-Live currently exposes no response cancellation command."""

        self._require_connection()

    async def truncate_response(self, item_id: str, audio_end_ms: int) -> None:
        """No-op: GPT-Live has no conversation-item truncation command."""

        self._require_connection()
        if audio_end_ms < 0:
            raise ValueError("audio_end_ms must be non-negative")

    async def append_commentary(self, content: str) -> None:
        """Append an application-owned directive for the Live model to speak."""

        if not content.strip():
            raise ValueError("commentary content is required")
        await self._require_connection().send(
            {
                "type": "session.commentary.append",
                "content": content,
                "delegation_id": None,
            }
        )

    async def append_thinking(self, content: str) -> None:
        """Add quiet application context without requesting spoken output."""

        if not content.strip():
            raise ValueError("thinking content is required")
        await self._require_connection().send(
            {
                "type": "session.thinking.append",
                "content": content,
                "delegation_id": None,
            }
        )

    async def append_instructions(self, content: str) -> int:
        """Append trusted wording and wait until the Live session accepts it.

        Acceptance is deliberately weaker than delivery: the transcript quality
        gate still verifies what was actually spoken. Waiting for the correlated
        acknowledgement closes the race where the caller could hear a response
        generated from the previous timeline before the directive was accepted.
        """

        if not content.strip():
            raise ValueError("instruction content is required")
        event_id = f"instruction-{uuid4().hex}"
        acknowledgement = asyncio.get_running_loop().create_future()
        self._pending_instruction_acks[event_id] = acknowledgement
        # Ownership starts when the trusted instruction is sent, not when its
        # acknowledgement happens to arrive.  Live may begin emitting the new
        # instructed response before session.instructions.appended; using the
        # acknowledgement-time sequence clips valid leading words.  Output that
        # was already observed before this send retains a lower sequence and is
        # still rejected by the actor's timeline cutoff.
        authorized_sequence = self._audio_sequence
        try:
            await self._require_connection().send(
                {
                    "type": "session.instructions.append",
                    "event_id": event_id,
                    "content": content,
                    "delegation_id": None,
                }
            )
            await asyncio.wait_for(
                acknowledgement, timeout=self._instruction_ack_timeout
            )
            return authorized_sequence
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                "GPT-Live did not acknowledge the appended instructions"
            ) from exc
        finally:
            self._pending_instruction_acks.pop(event_id, None)

    async def cancel_delegation(self, delegation_id: str, reason: str) -> None:
        """Invalidate a delegation locally so late server events cannot act."""

        self._require_connection()
        if not delegation_id:
            raise ValueError("delegation_id is required")
        self._cancelled_delegations.add(delegation_id)
        for operation_id, current_id in tuple(self._pending_operations.items()):
            if current_id == delegation_id:
                del self._pending_operations[operation_id]

    async def submit_tool_result(self, result: ToolResult) -> None:
        """Append one function result and explicitly continue the backend response."""

        connection = self._require_connection()
        delegation_id = self._pending_operations.get(result.operation_id)
        if delegation_id in self._cancelled_delegations:
            raise RuntimeError("cannot submit a result for a cancelled delegation")
        output = json.dumps(
            {
                "status": result.status.value,
                "payload": dict(result.payload),
                "error_class": result.error_class,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        await connection.send(
            {
                "type": "response.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": result.operation_id,
                    "output": output,
                },
            }
        )
        self._pending_operations.pop(result.operation_id, None)
        await connection.send({"type": "response.create"})

    async def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._closing = True
        try:
            if not self._closed_event.is_set():
                await connection.send({"type": "session.close"})
                if self._close_timeout:
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(
                            self._closed_event.wait(), timeout=self._close_timeout
                        )
        finally:
            for acknowledgement in self._pending_instruction_acks.values():
                if not acknowledgement.done():
                    acknowledgement.cancel()
            self._pending_instruction_acks.clear()
            await connection.close(reason="application_close")
            reader = self._reader_task
            if reader is not None and not reader.done():
                reader.cancel()
                with suppress(asyncio.CancelledError):
                    await reader
            self._reader_task = None
            delivery = self._delivery_task
            if delivery is not None and not delivery.done():
                delivery.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery
            self._delivery_task = None
            self._event_queue = None
            self._connection = None

    async def _open_connection(self) -> _LiveConnection:
        if self._connection_factory is not None:
            candidate = self._connection_factory()
            if inspect.isawaitable(candidate):
                candidate = await candidate
        else:
            client = self._client or AsyncOpenAI()
            candidate = client.live.connect()

        # The official SDK manager exposes enter(); tests may inject a direct
        # connection or an async factory returning one.
        enter = getattr(candidate, "enter", None)
        if enter is not None:
            candidate = await enter()
        elif not hasattr(candidate, "send"):
            aenter = getattr(candidate, "__aenter__", None)
            if aenter is None:
                raise TypeError("connection factory did not return a Live connection")
            candidate = await aenter()
        return cast(_LiveConnection, candidate)

    @staticmethod
    def _build_session_config(config: VoiceSessionConfig) -> dict[str, Any]:
        return {
            "model": config.frontend_model,
            "instructions": config.instructions,
            "audio": {"format": {"type": "audio/pcmu", "rate": 8000}},
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": config.backend_model,
                    "reasoning": {"effort": config.backend_reasoning_effort},
                    "instructions": config.backend_instructions,
                    "tools": [dict(tool) for tool in config.tools],
                    "tool_choice": "auto",
                    "parallel_tool_calls": False,
                },
            },
        }

    def _require_connection(self) -> _LiveConnection:
        if self._connection is None or self._closing:
            raise RuntimeError("GPT-Live voice engine is not connected")
        return self._connection

    async def _receive_loop(self, initial_events: list[Any]) -> None:
        assert self._connection is not None
        try:
            for event in initial_events:
                await self._handle_server_event(event)
            while not self._closed_event.is_set():
                event = await self._connection.recv()
                await self._handle_server_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closing:
                await self._emit(
                    SessionEvent("voice.error", {"reason": type(exc).__name__})
                )

    async def _handle_server_event(self, event: Any) -> None:
        data = _as_mapping(event)
        event_type = str(data.get("type", ""))

        if event_type == "session.started":
            session = _mapping_or_empty(data.get("session"))
            await self._emit(
                SessionEvent(
                    NormalizedEventKind.LIVE_SESSION_STARTED.value,
                    {"session_id": session.get("id")},
                )
            )
            return
        if event_type == "session.output_audio.delta":
            await self._emit_output_audio(data)
            return
        if event_type == "session.input_transcript.delta":
            await self._emit(SessionEvent("voice.input_transcript.delta", _transcript(data)))
            return
        if event_type == "session.output_transcript.delta":
            transcript = _transcript(data)
            transcript["audio_sequence"] = self._audio_sequence
            # Audio and transcript share one output ordering. Otherwise a
            # pre-ack transcript can carry the NEXT audio sequence and slip
            # through the acknowledgement cutoff as if it were authorized.
            self._audio_sequence += 1
            await self._emit(SessionEvent("voice.output_transcript.delta", transcript))
            return
        if event_type == "session.instructions.appended":
            client_event_id = str(data.get("client_event_id", ""))
            acknowledgement = self._pending_instruction_acks.get(client_event_id)
            if acknowledgement is not None and not acknowledgement.done():
                # Audio emitted before this sequence belongs to the older Live
                # timeline and must not leak after an application-owned gate.
                acknowledgement.set_result(self._audio_sequence)
            return
        if event_type == "session.delegation.created":
            delegation = _mapping_or_empty(data.get("delegation"))
            delegation_id = str(delegation.get("id", ""))
            if not delegation_id:
                raise ValueError("delegation.created event is missing delegation.id")
            await self._emit(
                SessionEvent(
                    NormalizedEventKind.DELEGATION_CREATED.value,
                    {"delegation_id": delegation_id},
                )
            )
            return
        if event_type == "response.event":
            await self._handle_response_event(data)
            return
        if event_type == "error":
            reason = _error_reason(data)
            client_event_id = str(data.get("client_event_id", ""))
            acknowledgement = self._pending_instruction_acks.get(client_event_id)
            if acknowledgement is not None and not acknowledgement.done():
                acknowledgement.set_exception(RuntimeError(reason))
            await self._emit(SessionEvent("voice.error", {"reason": reason}))
            return
        if event_type == "session.closed":
            self._closed_event.set()
            await self._emit(
                SessionEvent(
                    NormalizedEventKind.LIVE_SESSION_CLOSED.value,
                    {"reason": str(data.get("reason", "unknown"))},
                )
            )
            return
        if event_type in _PASSIVE_SESSION_EVENTS:
            return
        await self._emit(
            SessionEvent("provider.unknown_event", {"provider_type": event_type or "unknown"})
        )

    async def _emit_output_audio(self, data: Mapping[str, Any]) -> None:
        encoded = data.get("delta")
        if not isinstance(encoded, str):
            raise ValueError("output audio event is missing delta")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("output audio delta is not valid base64") from exc
        self._output_audio_bytes += len(payload)
        audio_end_ms = int(data.get("end_ms") or self._output_audio_bytes // 8)
        sequence = self._audio_sequence
        self._audio_sequence += 1
        await self._emit(
            SessionEvent(
                "voice.audio",
                {
                    "chunk": AudioChunk(payload, sequence, audio_end_ms),
                    "item_id": "gpt-live-output",
                    "audio_end_ms": audio_end_ms,
                },
            )
        )

    async def _handle_response_event(self, data: Mapping[str, Any]) -> None:
        delegation_id_value = data.get("delegation_id")
        delegation_id = (
            str(delegation_id_value) if delegation_id_value is not None else None
        )
        if delegation_id in self._cancelled_delegations:
            return
        nested = _mapping_or_empty(data.get("event"))
        nested_type = str(nested.get("type", ""))

        if nested_type == "response.created":
            if delegation_id is not None:
                response_id = _response_id(nested)
                if response_id:
                    self._active_response_ids[delegation_id] = response_id
                await self._emit(
                    SessionEvent(
                        NormalizedEventKind.BACKEND_RESPONSE_STARTED.value,
                        {"delegation_id": delegation_id},
                    )
                )
            return
        if nested_type == "response.output_item.done":
            item = _mapping_or_empty(nested.get("item"))
            if item.get("type") == "function_call":
                if delegation_id is not None:
                    response_id = (
                        _response_id(nested)
                        or self._active_response_ids.get(delegation_id, "")
                    )
                    if response_id:
                        self._tool_response_ids.add((delegation_id, response_id))
                await self._emit_tool_intent(item, delegation_id)
            return
        if nested_type == "response.completed":
            response_id = (
                _response_id(nested)
                or (
                    self._active_response_ids.get(delegation_id, "")
                    if delegation_id is not None
                    else ""
                )
            )
            if (
                delegation_id is not None
                and response_id
                and (delegation_id, response_id) in self._tool_response_ids
            ):
                # This completion belongs to the response that requested a tool.
                # The application may already have submitted the result and sent
                # response.create; only the later continuation can complete the
                # delegation.
                self._tool_response_ids.discard((delegation_id, response_id))
                return
            has_pending = any(
                current_id == delegation_id
                for current_id in self._pending_operations.values()
            )
            if delegation_id is not None and not has_pending:
                self._active_response_ids.pop(delegation_id, None)
                await self._emit(
                    SessionEvent(
                        NormalizedEventKind.DELEGATION_COMPLETED.value,
                        {"delegation_id": delegation_id},
                    )
                )
            return
        if nested_type in {"response.failed", "response.incomplete", "response.cancelled"}:
            if delegation_id is not None:
                await self._emit(
                    SessionEvent(
                        NormalizedEventKind.DELEGATION_CANCELLED.value,
                        {"delegation_id": delegation_id, "reason": nested_type},
                    )
                )
            return
        if nested_type in _PASSIVE_RESPONSE_EVENTS:
            return
        await self._emit(
            SessionEvent(
                "provider.unknown_event",
                {"provider_type": f"response.event:{nested_type or 'unknown'}"},
            )
        )

    async def _emit_tool_intent(
        self, item: Mapping[str, Any], delegation_id: str | None
    ) -> None:
        operation_id = str(item.get("call_id", ""))
        tool_name = str(item.get("name", ""))
        if not operation_id or not tool_name:
            raise ValueError("function call is missing call_id or name")
        raw_arguments = item.get("arguments", "{}")
        if not isinstance(raw_arguments, str):
            raise ValueError("function call arguments must be JSON text")
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("function call arguments must decode to an object")
        self._pending_operations[operation_id] = delegation_id
        intent = ToolIntent(
            operation_id=operation_id,
            tool_name=tool_name,
            operation_kind=(
                OperationKind.WRITE if tool_name in _WRITE_TOOLS else OperationKind.READ
            ),
            arguments=arguments,
            delegation_id=delegation_id,
        )
        await self._emit(
            SessionEvent(
                NormalizedEventKind.BACKEND_TOOL_REQUESTED.value,
                {"intent": intent},
            )
        )

    async def _delivery_loop(self) -> None:
        queue = self._event_queue
        assert queue is not None
        while True:
            event = await queue.get()
            try:
                await self._on_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if event.kind == "voice.error":
                    continue
                with suppress(Exception):
                    await self._on_event(
                        SessionEvent(
                            "voice.error",
                            {"reason": f"EventDelivery{type(exc).__name__}"},
                        )
                    )

    async def _emit(self, event: SessionEvent) -> None:
        queue = self._event_queue
        if queue is None:
            raise RuntimeError("GPT-Live event delivery is not running")
        queue.put_nowait(event)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if model_dump is None:
        raise TypeError("Live event must be a mapping or SDK model")
    dumped = model_dump(mode="python", by_alias=True, exclude_none=True)
    if not isinstance(dumped, Mapping):
        raise TypeError("Live SDK event did not serialize to a mapping")
    return dumped


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    return _as_mapping(value)


def _transcript(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "delta": str(data.get("delta", "")),
        "start_ms": int(data.get("start_ms", 0)),
        "end_ms": int(data.get("end_ms", 0)),
    }


def _error_reason(data: Mapping[str, Any]) -> str:
    error = _mapping_or_empty(data.get("error"))
    return str(error.get("message") or error.get("code") or "openai_live_error")


def _response_id(event: Mapping[str, Any]) -> str:
    direct = str(event.get("response_id", "")).strip()
    if direct:
        return direct
    response = _mapping_or_empty(event.get("response"))
    return str(response.get("id", "")).strip()
