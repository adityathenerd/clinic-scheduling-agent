"""Twilio Programmable Voice Media Streams transport boundary.

The parser is deliberately strict for fields that influence audio processing,
while unknown event types remain non-fatal.  This lets the WebSocket loop
record provider evolution without accidentally interpreting malformed audio.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from twilio.request_validator import RequestValidator

from clinic_agent.call_mechanics.models import (
    AudioChunk,
    CallDescriptor,
    CallDirection,
    SessionEvent,
)


MULAW_ENCODING = "audio/x-mulaw"
MULAW_SAMPLE_RATE = 8_000
MULAW_CHANNELS = 1
DEFAULT_MAX_AUDIO_PAYLOAD_BYTES = 64 * 1024
DEFAULT_MAX_MESSAGE_BYTES = 128 * 1024
MAX_MARK_LABEL_CHARACTERS = 256
DEFAULT_SPEECH_RMS_THRESHOLD = 600
DEFAULT_SPEECH_HANGOVER_MS = 700


class TwilioProtocolError(ValueError):
    """A known Twilio event violated the Media Streams wire contract."""


@dataclass(frozen=True, slots=True)
class TwilioMediaFormat:
    encoding: str
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class TwilioConnectedEvent:
    protocol: str
    version: str

    @property
    def actor_event(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class TwilioStartEvent:
    stream_sid: str
    call_sid: str
    tracks: tuple[str, ...]
    media_format: TwilioMediaFormat
    custom_parameters: Mapping[str, str] = field(default_factory=dict)

    @property
    def actor_event(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class TwilioMediaEvent:
    stream_sid: str
    track: str
    chunk: AudioChunk

    @property
    def actor_event(self) -> SessionEvent:
        return SessionEvent("telephony.audio", {"chunk": self.chunk})


@dataclass(frozen=True, slots=True)
class TwilioMarkEvent:
    stream_sid: str
    label: str
    item_id: str | None
    audio_end_ms: int | None

    @property
    def actor_event(self) -> SessionEvent | None:
        if self.item_id is None or self.audio_end_ms is None:
            return None
        return SessionEvent(
            "playback.marked",
            {"item_id": self.item_id, "audio_end_ms": self.audio_end_ms},
        )


@dataclass(frozen=True, slots=True)
class TwilioStopEvent:
    stream_sid: str
    call_sid: str

    @property
    def actor_event(self) -> SessionEvent:
        return SessionEvent("telephony.stop", {"reason": "twilio_stop"})


@dataclass(frozen=True, slots=True)
class UnknownTwilioEvent:
    """A safe representation that does not retain an unbounded raw message."""

    event_type: str

    @property
    def actor_event(self) -> None:
        return None


TwilioInboundEvent = (
    TwilioConnectedEvent
    | TwilioStartEvent
    | TwilioMediaEvent
    | TwilioMarkEvent
    | TwilioStopEvent
    | UnknownTwilioEvent
)


def encode_playback_mark(item_id: str, audio_end_ms: int) -> str:
    """Encode the playback cursor echoed by Twilio's ``mark`` event."""

    if not item_id or ":" in item_id:
        raise ValueError("item_id must be non-empty and must not contain ':'")
    if audio_end_ms < 0:
        raise ValueError("audio_end_ms must be non-negative")
    label = f"{item_id}:{audio_end_ms}"
    if len(label) > MAX_MARK_LABEL_CHARACTERS:
        raise ValueError("playback mark label is too long")
    return label


def decode_playback_mark(label: str) -> tuple[str, int]:
    """Decode an application playback cursor, rejecting foreign labels."""

    if not label or len(label) > MAX_MARK_LABEL_CHARACTERS:
        raise ValueError("invalid playback mark label")
    item_id, separator, raw_ms = label.rpartition(":")
    if not separator or not item_id or ":" in item_id:
        raise ValueError("invalid playback mark label")
    try:
        audio_end_ms = int(raw_ms)
    except ValueError as exc:
        raise ValueError("playback mark timestamp must be an integer") from exc
    if audio_end_ms < 0:
        raise ValueError("playback mark timestamp must be non-negative")
    return item_id, audio_end_ms


def parse_twilio_event(
    message: str | bytes | bytearray | Mapping[str, Any],
    *,
    max_audio_payload_bytes: int = DEFAULT_MAX_AUDIO_PAYLOAD_BYTES,
    max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
) -> TwilioInboundEvent:
    """Parse one Twilio WebSocket message into a bounded typed value."""

    if max_audio_payload_bytes <= 0 or max_message_bytes <= 0:
        raise ValueError("message and audio bounds must be positive")
    payload = _load_message(message, max_message_bytes=max_message_bytes)
    event_type = payload.get("event")
    if not isinstance(event_type, str) or not event_type:
        raise TwilioProtocolError("Twilio message requires a non-empty event")

    if event_type == "connected":
        return TwilioConnectedEvent(
            protocol=_required_string(payload, "protocol"),
            version=_required_string(payload, "version"),
        )
    if event_type == "start":
        event = _parse_start(payload)
        envelope_stream_sid = payload.get("streamSid")
        if envelope_stream_sid is not None and envelope_stream_sid != event.stream_sid:
            raise TwilioProtocolError("start streamSid does not match message streamSid")
        return event
    if event_type == "media":
        return _parse_media(payload, max_audio_payload_bytes=max_audio_payload_bytes)
    if event_type == "mark":
        return _parse_mark(payload)
    if event_type == "stop":
        stop = _required_mapping(payload, "stop")
        return TwilioStopEvent(
            stream_sid=_required_string(payload, "streamSid"),
            call_sid=_required_string(stop, "callSid"),
        )
    return UnknownTwilioEvent(event_type=event_type)


def _load_message(
    message: str | bytes | bytearray | Mapping[str, Any], *, max_message_bytes: int
) -> Mapping[str, Any]:
    if isinstance(message, Mapping):
        return message
    if isinstance(message, str):
        if len(message.encode("utf-8")) > max_message_bytes:
            raise TwilioProtocolError("Twilio message exceeds configured size")
        raw = message
    elif isinstance(message, (bytes, bytearray)):
        if len(message) > max_message_bytes:
            raise TwilioProtocolError("Twilio message exceeds configured size")
        try:
            raw = bytes(message).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TwilioProtocolError("Twilio message must be UTF-8 JSON") from exc
    else:
        raise TypeError("Twilio message must be JSON text, bytes, or a mapping")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TwilioProtocolError("Twilio message is not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise TwilioProtocolError("Twilio message must be a JSON object")
    return decoded


def _parse_start(payload: Mapping[str, Any]) -> TwilioStartEvent:
    start = _required_mapping(payload, "start")
    media = _required_mapping(start, "mediaFormat")
    media_format = TwilioMediaFormat(
        encoding=_required_string(media, "encoding"),
        sample_rate=_required_int(media, "sampleRate"),
        channels=_required_int(media, "channels"),
    )
    expected = TwilioMediaFormat(MULAW_ENCODING, MULAW_SAMPLE_RATE, MULAW_CHANNELS)
    if media_format != expected:
        raise TwilioProtocolError(
            "unsupported media format; expected audio/x-mulaw at 8000 Hz mono"
        )
    raw_tracks = start.get("tracks", ())
    if not isinstance(raw_tracks, list) or not all(
        isinstance(track, str) and track for track in raw_tracks
    ):
        raise TwilioProtocolError("start.tracks must be a list of non-empty strings")
    raw_parameters = start.get("customParameters", {})
    if not isinstance(raw_parameters, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_parameters.items()
    ):
        raise TwilioProtocolError("start.customParameters must contain string values")
    return TwilioStartEvent(
        stream_sid=_required_string(start, "streamSid"),
        call_sid=_required_string(start, "callSid"),
        tracks=tuple(raw_tracks),
        media_format=media_format,
        custom_parameters=dict(raw_parameters),
    )


def _parse_media(
    payload: Mapping[str, Any], *, max_audio_payload_bytes: int
) -> TwilioMediaEvent:
    media = _required_mapping(payload, "media")
    encoded = _required_string(media, "payload")
    # Reject oversized input before decoding to keep attacker-controlled
    # allocation bounded. Four base64 characters encode at most three bytes.
    max_encoded_length = ((max_audio_payload_bytes + 2) // 3) * 4
    if len(encoded) > max_encoded_length:
        raise TwilioProtocolError("Twilio audio payload exceeds configured size")
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TwilioProtocolError("Twilio audio payload is not strict base64") from exc
    if not audio:
        raise TwilioProtocolError("Twilio audio payload must not be empty")
    if len(audio) > max_audio_payload_bytes:
        raise TwilioProtocolError("Twilio audio payload exceeds configured size")
    return TwilioMediaEvent(
        stream_sid=_required_string(payload, "streamSid"),
        track=_required_string(media, "track"),
        chunk=AudioChunk(
            payload=audio,
            sequence=_required_non_negative_int(media, "chunk"),
            timestamp_ms=_required_non_negative_int(media, "timestamp"),
        ),
    )


def _parse_mark(payload: Mapping[str, Any]) -> TwilioMarkEvent:
    mark = _required_mapping(payload, "mark")
    label = _required_string(mark, "name")
    try:
        item_id, audio_end_ms = decode_playback_mark(label)
    except ValueError:
        # A foreign or legacy label is harmless: expose it for telemetry but do
        # not advance the actor's trusted playback cursor.
        item_id, audio_end_ms = None, None
    return TwilioMarkEvent(
        stream_sid=_required_string(payload, "streamSid"),
        label=label,
        item_id=item_id,
        audio_end_ms=audio_end_ms,
    )


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise TwilioProtocolError(f"{key} must be an object")
    return nested


def _required_string(value: Mapping[str, Any], key: str) -> str:
    field_value = value.get(key)
    if not isinstance(field_value, str) or not field_value:
        raise TwilioProtocolError(f"{key} must be a non-empty string")
    return field_value


def _required_int(value: Mapping[str, Any], key: str) -> int:
    field_value = value.get(key)
    if isinstance(field_value, bool):
        raise TwilioProtocolError(f"{key} must be an integer")
    if not isinstance(field_value, (int, str)):
        raise TwilioProtocolError(f"{key} must be an integer")
    try:
        return int(field_value)
    except (TypeError, ValueError) as exc:
        raise TwilioProtocolError(f"{key} must be an integer") from exc


def _required_non_negative_int(value: Mapping[str, Any], key: str) -> int:
    result = _required_int(value, key)
    if result < 0:
        raise TwilioProtocolError(f"{key} must be non-negative")
    return result


JsonSender = Callable[[Mapping[str, Any]], Awaitable[None]]
TransferHandler = Callable[[str], Awaitable[None]]
CloseHandler = Callable[[str], Awaitable[None]]


class TwilioTelephonyAdapter:
    """Outbound half of a bidirectional Twilio Media Stream.

    The supplied sender is normally ``WebSocket.send_json``. Transfer is kept
    injectable because changing an active call requires the application's
    Twilio REST/TwiML policy rather than a Media Streams WebSocket message.
    """

    def __init__(
        self,
        stream_sid: str,
        sender: JsonSender,
        *,
        transfer_handler: TransferHandler | None = None,
        close_handler: CloseHandler | None = None,
        max_audio_payload_bytes: int = DEFAULT_MAX_AUDIO_PAYLOAD_BYTES,
    ) -> None:
        if not stream_sid:
            raise ValueError("stream_sid is required")
        if max_audio_payload_bytes <= 0:
            raise ValueError("max_audio_payload_bytes must be positive")
        self.stream_sid = stream_sid
        self._sender = sender
        self._transfer_handler = transfer_handler
        self._close_handler = close_handler
        self._max_audio_payload_bytes = max_audio_payload_bytes
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def send_audio(self, chunk: AudioChunk) -> None:
        if not isinstance(chunk, AudioChunk):
            raise TypeError("send_audio requires an AudioChunk")
        if not chunk.payload:
            raise ValueError("audio payload must not be empty")
        if len(chunk.payload) > self._max_audio_payload_bytes:
            raise ValueError("audio payload exceeds configured size")
        await self._send(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(chunk.payload).decode("ascii")},
            }
        )

    async def clear_playback(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def mark_playback(self, label: str) -> None:
        # Round-trip validation keeps marks compatible with the inbound parser.
        item_id, audio_end_ms = decode_playback_mark(label)
        normalized_label = encode_playback_mark(item_id, audio_end_ms)
        await self._send(
            {
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": normalized_label},
            }
        )

    async def transfer(self, destination: str) -> None:
        if self._closed:
            raise RuntimeError("Twilio transport is closed")
        if not destination:
            raise ValueError("transfer destination is required")
        if self._transfer_handler is None:
            raise NotImplementedError("Twilio transfer handler is not configured")
        await self._transfer_handler(destination)

    async def close(self, reason: str) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._close_handler is not None:
                await self._close_handler(reason)

    async def _send(self, message: Mapping[str, Any]) -> None:
        async with self._send_lock:
            if self._closed:
                raise RuntimeError("Twilio transport is closed")
            await self._sender(message)


class TwilioMediaSession:
    """Bind one accepted Twilio WebSocket to its call descriptor and adapter."""

    def __init__(
        self,
        websocket: Any,
        start: TwilioStartEvent,
        *,
        direction: CallDirection = CallDirection.INBOUND,
        speech_rms_threshold: int = DEFAULT_SPEECH_RMS_THRESHOLD,
        speech_hangover_ms: int = DEFAULT_SPEECH_HANGOVER_MS,
    ) -> None:
        self.websocket = websocket
        self.start = start
        self.descriptor = CallDescriptor(
            start.call_sid,
            direction,
            destination=("provider-connected" if direction is CallDirection.OUTBOUND else None),
            context_reference=start.custom_parameters.get("context_reference") or None,
        )
        if speech_rms_threshold < 0 or speech_hangover_ms < 0:
            raise ValueError("speech detector settings must be non-negative")
        self._speech_rms_threshold = speech_rms_threshold
        self._speech_hangover_ms = speech_hangover_ms
        self._speech_active = False
        self._last_speech_timestamp_ms = 0
        self._pending_actor_events: list[SessionEvent] = []
        self._adapter = TwilioTelephonyAdapter(
            start.stream_sid,
            websocket.send_json,
            close_handler=self._close_websocket,
        )

    @classmethod
    async def create(
        cls,
        websocket: Any,
        *,
        startup_timeout: float = 5.0,
        max_preamble_events: int = 4,
    ) -> "TwilioMediaSession":
        """Read the bounded connected/start preamble before creating an actor."""

        if startup_timeout <= 0 or max_preamble_events <= 0:
            raise ValueError("startup bounds must be positive")
        for _ in range(max_preamble_events):
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=startup_timeout
            )
            event = parse_twilio_event(raw)
            if isinstance(event, TwilioStartEvent):
                direction = (
                    CallDirection.OUTBOUND
                    if event.custom_parameters.get("direction") == "outbound"
                    else CallDirection.INBOUND
                )
                return cls(websocket, event, direction=direction)
            if not isinstance(event, (TwilioConnectedEvent, UnknownTwilioEvent)):
                raise TwilioProtocolError("Twilio stream sent media before start")
        raise TwilioProtocolError("Twilio stream did not send start within preamble bound")

    async def receive_event(self) -> SessionEvent | None:
        if self._pending_actor_events:
            return self._pending_actor_events.pop(0)
        event = parse_twilio_event(await self.websocket.receive_text())
        stream_sid = getattr(event, "stream_sid", self.start.stream_sid)
        if stream_sid != self.start.stream_sid:
            raise TwilioProtocolError("event streamSid does not match active stream")
        actor_event = event.actor_event
        if isinstance(event, TwilioMediaEvent):
            is_speech = _mulaw_rms(event.chunk.payload) >= self._speech_rms_threshold
            if is_speech:
                self._last_speech_timestamp_ms = event.chunk.timestamp_ms
                if not self._speech_active:
                    self._speech_active = True
                    self._pending_actor_events.append(actor_event)
                    return SessionEvent("caller.speech_started")
            elif (
                self._speech_active
                and event.chunk.timestamp_ms - self._last_speech_timestamp_ms
                >= self._speech_hangover_ms
            ):
                self._speech_active = False
                self._pending_actor_events.append(actor_event)
                return SessionEvent("caller.speech_stopped")
        return actor_event

    async def send_audio(self, chunk: AudioChunk) -> None:
        await self._adapter.send_audio(chunk)

    async def clear_playback(self) -> None:
        await self._adapter.clear_playback()

    async def mark_playback(self, label: str) -> None:
        await self._adapter.mark_playback(label)

    async def transfer(self, destination: str) -> None:
        await self._adapter.transfer(destination)

    async def close(self, reason: str) -> None:
        await self._adapter.close(reason)

    async def _close_websocket(self, reason: str) -> None:
        try:
            await self.websocket.close(code=1000, reason=reason[:123])
        except RuntimeError:
            # Starlette raises when the peer has already closed the connection.
            return


def _mulaw_rms(payload: bytes) -> int:
    """Compute signal RMS directly from G.711 μ-law bytes without transcoding."""

    if not payload:
        return 0
    total = 0
    for encoded in payload:
        value = (~encoded) & 0xFF
        sign = value & 0x80
        exponent = (value >> 4) & 0x07
        mantissa = value & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if sign:
            sample = -sample
        total += sample * sample
    return int(math.sqrt(total / len(payload)))


def verify_twilio_signature(
    *,
    auth_token: str,
    url: str,
    signature: str | None,
    params: Mapping[str, Any] | None = None,
) -> bool:
    """Validate an HTTP webhook or WebSocket upgrade signature, fail closed."""

    if not auth_token or not url or not signature:
        return False
    try:
        return bool(RequestValidator(auth_token).validate(url, params or {}, signature))
    except (TypeError, ValueError):
        return False
