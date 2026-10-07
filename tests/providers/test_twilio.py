from __future__ import annotations

import base64
import json
import unittest

from twilio.request_validator import RequestValidator

from clinic_agent.call_mechanics.models import AudioChunk
from clinic_agent.providers.twilio import (
    TwilioConnectedEvent,
    TwilioMarkEvent,
    TwilioMediaEvent,
    TwilioMediaFormat,
    TwilioMediaSession,
    TwilioProtocolError,
    TwilioStartEvent,
    TwilioStopEvent,
    TwilioTelephonyAdapter,
    UnknownTwilioEvent,
    decode_playback_mark,
    encode_playback_mark,
    parse_twilio_event,
    verify_twilio_signature,
)


class TwilioParserTests(unittest.TestCase):
    def test_parses_connected_and_valid_start(self) -> None:
        connected = parse_twilio_event(
            '{"event":"connected","protocol":"Call","version":"1.0.0"}'
        )
        self.assertIsInstance(connected, TwilioConnectedEvent)

        start = parse_twilio_event(
            {
                "event": "start",
                "streamSid": "MZ123",
                "start": {
                    "streamSid": "MZ123",
                    "callSid": "CA123",
                    "tracks": ["inbound"],
                    "customParameters": {"tenant": "clinic-1"},
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                },
            }
        )
        self.assertIsInstance(start, TwilioStartEvent)
        assert isinstance(start, TwilioStartEvent)
        self.assertEqual(start.stream_sid, "MZ123")
        self.assertEqual(start.call_sid, "CA123")
        self.assertEqual(start.custom_parameters, {"tenant": "clinic-1"})

    def test_rejects_any_start_format_other_than_mulaw_8khz_mono(self) -> None:
        for media_format in (
            {"encoding": "audio/pcm", "sampleRate": 8000, "channels": 1},
            {"encoding": "audio/x-mulaw", "sampleRate": 16000, "channels": 1},
            {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 2},
        ):
            with self.subTest(media_format=media_format), self.assertRaises(TwilioProtocolError):
                parse_twilio_event(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZ1",
                            "callSid": "CA1",
                            "tracks": ["inbound"],
                            "mediaFormat": media_format,
                        },
                    }
                )

    def test_media_becomes_actor_audio_event(self) -> None:
        audio = b"\xff\x7f\x00"
        event = parse_twilio_event(
            {
                "event": "media",
                "streamSid": "MZ123",
                "media": {
                    "track": "inbound",
                    "chunk": "7",
                    "timestamp": "120",
                    "payload": base64.b64encode(audio).decode("ascii"),
                },
            }
        )
        self.assertIsInstance(event, TwilioMediaEvent)
        assert isinstance(event, TwilioMediaEvent)
        self.assertEqual(event.chunk, AudioChunk(audio, sequence=7, timestamp_ms=120))
        self.assertEqual(event.actor_event.kind, "telephony.audio")
        self.assertIs(event.actor_event.data["chunk"], event.chunk)

    def test_media_base64_is_strict_and_size_bounded(self) -> None:
        template = {
            "event": "media",
            "streamSid": "MZ123",
            "media": {"track": "inbound", "chunk": "1", "timestamp": "0"},
        }
        for encoded in ("not base64!", "", "YQ="):
            message = json.loads(json.dumps(template))
            message["media"]["payload"] = encoded
            with self.subTest(encoded=encoded), self.assertRaises(TwilioProtocolError):
                parse_twilio_event(message)

        message = json.loads(json.dumps(template))
        message["media"]["payload"] = base64.b64encode(b"abcd").decode("ascii")
        with self.assertRaises(TwilioProtocolError):
            parse_twilio_event(message, max_audio_payload_bytes=3)

    def test_mark_decoding_only_advances_cursor_for_our_label(self) -> None:
        mark = parse_twilio_event(
            {
                "event": "mark",
                "streamSid": "MZ123",
                "mark": {"name": "item_123:640"},
            }
        )
        self.assertIsInstance(mark, TwilioMarkEvent)
        assert isinstance(mark, TwilioMarkEvent)
        self.assertEqual((mark.item_id, mark.audio_end_ms), ("item_123", 640))
        self.assertEqual(mark.actor_event.kind, "playback.marked")

        foreign = parse_twilio_event(
            {"event": "mark", "streamSid": "MZ123", "mark": {"name": "foreign"}}
        )
        assert isinstance(foreign, TwilioMarkEvent)
        self.assertIsNone(foreign.actor_event)

    def test_stop_and_unknown_are_safe(self) -> None:
        stopped = parse_twilio_event(
            {
                "event": "stop",
                "streamSid": "MZ123",
                "stop": {"callSid": "CA123", "accountSid": "AC123"},
            }
        )
        self.assertIsInstance(stopped, TwilioStopEvent)
        assert isinstance(stopped, TwilioStopEvent)
        self.assertEqual(stopped.actor_event.kind, "telephony.stop")

        unknown = parse_twilio_event({"event": "future-provider-event", "large": "ignored"})
        self.assertEqual(unknown, UnknownTwilioEvent("future-provider-event"))

    def test_json_input_is_bounded_and_malformed_known_event_fails(self) -> None:
        with self.assertRaises(TwilioProtocolError):
            parse_twilio_event("{" + (" " * 100) + "}", max_message_bytes=10)
        with self.assertRaises(TwilioProtocolError):
            parse_twilio_event({"event": "media"})

    def test_mark_codec_round_trip_and_rejects_ambiguous_ids(self) -> None:
        label = encode_playback_mark("item_abc", 421)
        self.assertEqual(label, "item_abc:421")
        self.assertEqual(decode_playback_mark(label), ("item_abc", 421))
        with self.assertRaises(ValueError):
            encode_playback_mark("bad:id", 10)
        with self.assertRaises(ValueError):
            decode_playback_mark("item:-1")


class TwilioAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_media_mark_and_clear_with_bound_stream_sid(self) -> None:
        messages: list[dict[str, object]] = []

        async def send(message: object) -> None:
            messages.append(dict(message))  # type: ignore[arg-type]

        adapter = TwilioTelephonyAdapter("MZ123", send)
        await adapter.send_audio(AudioChunk(b"\x01\x02", sequence=1, timestamp_ms=0))
        await adapter.mark_playback("item_1:40")
        await adapter.clear_playback()

        self.assertEqual(
            messages,
            [
                {
                    "event": "media",
                    "streamSid": "MZ123",
                    "media": {"payload": "AQI="},
                },
                {
                    "event": "mark",
                    "streamSid": "MZ123",
                    "mark": {"name": "item_1:40"},
                },
                {"event": "clear", "streamSid": "MZ123"},
            ],
        )

    async def test_close_is_idempotent_and_blocks_further_sends(self) -> None:
        close_reasons: list[str] = []

        async def send(message: object) -> None:
            raise AssertionError("send should not be called")

        async def close(reason: str) -> None:
            close_reasons.append(reason)

        adapter = TwilioTelephonyAdapter("MZ123", send, close_handler=close)
        await adapter.close("caller_hangup")
        await adapter.close("duplicate")
        self.assertEqual(close_reasons, ["caller_hangup"])
        self.assertTrue(adapter.closed)
        with self.assertRaises(RuntimeError):
            await adapter.clear_playback()

    async def test_transfer_is_explicitly_injected(self) -> None:
        destinations: list[str] = []

        async def send(message: object) -> None:
            pass

        async def transfer(destination: str) -> None:
            destinations.append(destination)

        adapter = TwilioTelephonyAdapter("MZ123", send, transfer_handler=transfer)
        await adapter.transfer("+15551234567")
        self.assertEqual(destinations, ["+15551234567"])

        without_transfer = TwilioTelephonyAdapter("MZ123", send)
        with self.assertRaises(NotImplementedError):
            await without_transfer.transfer("+15551234567")


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = list(messages)
        self.sent: list[dict[str, object]] = []
        self.closed: list[tuple[int, str]] = []

    async def receive_text(self) -> str:
        if not self.messages:
            raise RuntimeError("no message")
        return self.messages.pop(0)

    async def send_json(self, message: object) -> None:
        self.sent.append(dict(message))  # type: ignore[arg-type]

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


class TwilioMediaSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_consumes_preamble_and_normalizes_media(self) -> None:
        websocket = FakeWebSocket(
            [
                '{"event":"connected","protocol":"Call","version":"1.0.0"}',
                json.dumps(
                    {
                        "event": "start",
                        "start": {
                            "streamSid": "MZ1",
                            "callSid": "CA1",
                            "tracks": ["inbound"],
                            "customParameters": {
                                "direction": "outbound",
                                "context_reference": "confirmation-call-0001",
                            },
                            "mediaFormat": {
                                "encoding": "audio/x-mulaw",
                                "sampleRate": 8000,
                                "channels": 1,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": "MZ1",
                        "media": {
                            "track": "inbound",
                            "chunk": "1",
                            "timestamp": "20",
                            "payload": "/w==",
                        },
                    }
                ),
            ]
        )
        session = await TwilioMediaSession.create(websocket)
        self.assertEqual("CA1", session.descriptor.provider_call_id)
        self.assertEqual("outbound", session.descriptor.direction.value)
        self.assertEqual(
            "confirmation-call-0001", session.descriptor.context_reference
        )
        event = await session.receive_event()
        self.assertEqual("telephony.audio", event.kind)
        self.assertEqual(b"\xff", event.data["chunk"].payload)

    async def test_rejects_stream_sid_change(self) -> None:
        start = TwilioStartEvent(
            "MZ1",
            "CA1",
            ("inbound",),
            TwilioMediaFormat("audio/x-mulaw", 8000, 1),
        )
        websocket = FakeWebSocket(
            [
                json.dumps(
                    {
                        "event": "stop",
                        "streamSid": "MZ-other",
                        "stop": {"callSid": "CA1"},
                    }
                )
            ]
        )
        session = TwilioMediaSession(websocket, start)
        with self.assertRaisesRegex(TwilioProtocolError, "streamSid"):
            await session.receive_event()

    async def test_detects_speech_edge_before_forwarding_loud_audio(self) -> None:
        start = TwilioStartEvent(
            "MZ1",
            "CA1",
            ("inbound",),
            TwilioMediaFormat("audio/x-mulaw", 8000, 1),
        )
        websocket = FakeWebSocket(
            [
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": "MZ1",
                        "media": {
                            "track": "inbound",
                            "chunk": "1",
                            "timestamp": "20",
                            "payload": base64.b64encode(b"\x00" * 160).decode("ascii"),
                        },
                    }
                )
            ]
        )
        session = TwilioMediaSession(websocket, start)
        speech = await session.receive_event()
        audio = await session.receive_event()
        self.assertEqual("caller.speech_started", speech.kind)
        self.assertEqual("telephony.audio", audio.kind)

    async def test_emits_speech_stop_after_hangover_before_forwarding_silence(self) -> None:
        start = TwilioStartEvent(
            "MZ1",
            "CA1",
            ("inbound",),
            TwilioMediaFormat("audio/x-mulaw", 8000, 1),
        )
        websocket = FakeWebSocket(
            [
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": "MZ1",
                        "media": {
                            "track": "inbound",
                            "chunk": "1",
                            "timestamp": "20",
                            "payload": base64.b64encode(b"\x00" * 160).decode("ascii"),
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": "MZ1",
                        "media": {
                            "track": "inbound",
                            "chunk": "2",
                            "timestamp": "740",
                            "payload": base64.b64encode(b"\xff" * 160).decode("ascii"),
                        },
                    }
                ),
            ]
        )
        session = TwilioMediaSession(websocket, start)

        self.assertEqual("caller.speech_started", (await session.receive_event()).kind)
        self.assertEqual("telephony.audio", (await session.receive_event()).kind)
        self.assertEqual("caller.speech_stopped", (await session.receive_event()).kind)
        self.assertEqual("telephony.audio", (await session.receive_event()).kind)


class TwilioSignatureTests(unittest.TestCase):
    def test_signature_verification_matches_twilio_sdk(self) -> None:
        token = "test-auth-token"
        url = "https://voice.example.test/twilio/incoming"
        params = {"CallSid": "CA123", "From": "+15551234567"}
        signature = RequestValidator(token).compute_signature(url, params)

        self.assertTrue(
            verify_twilio_signature(
                auth_token=token,
                url=url,
                params=params,
                signature=signature,
            )
        )
        self.assertFalse(
            verify_twilio_signature(
                auth_token=token,
                url=url,
                params=params,
                signature="tampered",
            )
        )
        self.assertFalse(
            verify_twilio_signature(
                auth_token="",
                url=url,
                params=params,
                signature=signature,
            )
        )


if __name__ == "__main__":
    unittest.main()
