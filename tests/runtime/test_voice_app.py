from __future__ import annotations

import asyncio
from dataclasses import dataclass
import unittest
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clinic_agent.call_mechanics import CallDescriptor, CallDirection, SessionEvent
from clinic_agent.runtime.config import VoiceRuntimeSettings
from clinic_agent.runtime.voice_app import (
    MediaBoundCallSupervisor,
    create_voice_app,
    public_media_websocket_url,
)


def _settings() -> VoiceRuntimeSettings:
    return VoiceRuntimeSettings(
        openai_api_key="not-a-real-key",
        twilio_account_sid="AC-not-real",
        twilio_auth_token="not-a-real-token",
        twilio_phone_number="+14155550100",
        public_base_url="https://voice.example.test/base",
    )


class _Verifier:
    def __init__(self, valid: bool = True) -> None:
        self.valid = valid
        self.calls: list[tuple[str, dict[str, str], str]] = []

    def validate(self, url: str, params: dict[str, str], signature: str) -> bool:
        self.calls.append((url, dict(params), signature))
        return self.valid


class _FakeMediaSession:
    def __init__(
        self,
        call_id: str,
        events: list[SessionEvent] | None = None,
        *,
        receive_error: Exception | None = None,
    ) -> None:
        self.descriptor = CallDescriptor(call_id, CallDirection.INBOUND)
        self.events = list(events or [])
        self.close_reasons: list[str] = []
        self.receive_error = receive_error

    async def receive_event(self) -> SessionEvent | None:
        if self.receive_error is not None:
            error = self.receive_error
            self.receive_error = None
            raise error
        if self.events:
            return self.events.pop(0)
        return None

    async def send_audio(self, chunk: object) -> None:
        return None

    async def clear_playback(self) -> None:
        return None

    async def mark_playback(self, label: str) -> None:
        return None

    async def transfer(self, destination: str) -> None:
        return None

    async def close(self, reason: str) -> None:
        self.close_reasons.append(reason)


class _FakeActor:
    def __init__(self, descriptor: CallDescriptor) -> None:
        self.descriptor = descriptor
        self.done = asyncio.Event()
        self.start_count = 0
        self.events: list[SessionEvent] = []
        self.close_reasons: list[str] = []

    async def start(self) -> "_FakeActor":
        self.start_count += 1
        return self

    async def dispatch(self, event: SessionEvent) -> None:
        self.events.append(event)
        if event.kind == "telephony.stop":
            self.done.set()

    async def close(self, reason: str = "requested") -> None:
        self.close_reasons.append(reason)
        self.done.set()


class VoiceWebhookTests(unittest.TestCase):
    def test_health_check_does_not_require_provider_credentials(self) -> None:
        app = create_voice_app(_settings(), signature_verifier=_Verifier())
        with TestClient(app) as client:
            response = client.get("/healthz")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())

    def test_signed_webhook_uses_exact_public_url_and_privacy_minimal_twiml(self) -> None:
        verifier = _Verifier()
        app = create_voice_app(
            _settings(),
            signature_verifier=verifier,
            session_id_factory=lambda: "opaque-session-123",
        )
        form = {
            "CallSid": "CA123",
            "From": "+14155550199",
            "To": "+14155550100",
            "Direction": "inbound",
        }
        with TestClient(app) as client:
            response = client.post(
                "/twilio/voice?ignored=proxy-value",
                data=form,
                headers={"X-Twilio-Signature": "valid-signature"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [
                (
                    "https://voice.example.test/base/twilio/voice",
                    form,
                    "valid-signature",
                )
            ],
            verifier.calls,
        )
        root = ElementTree.fromstring(response.text)
        stream = root.find("./Connect/Stream")
        assert stream is not None
        self.assertEqual(
            "wss://voice.example.test/base/twilio/media",
            stream.attrib["url"],
        )
        parameters = root.findall("./Connect/Stream/Parameter")
        self.assertEqual(
            [
                {"name": "session_id", "value": "opaque-session-123"},
                {"name": "direction", "value": "inbound"},
            ],
            [item.attrib for item in parameters],
        )
        self.assertNotIn(form["From"], response.text)
        self.assertNotIn(form["To"], response.text)

    def test_rejects_invalid_signature_without_emitting_twiml(self) -> None:
        app = create_voice_app(_settings(), signature_verifier=_Verifier(False))
        with TestClient(app) as client:
            response = client.post(
                "/twilio/voice",
                data={"CallSid": "CA123"},
                headers={"X-Twilio-Signature": "bad"},
            )
        self.assertEqual(403, response.status_code)
        self.assertNotIn("<Stream", response.text)

    def test_confirmation_context_is_signed_and_forwarded_as_opaque_stream_parameter(self) -> None:
        verifier = _Verifier()
        app = create_voice_app(
            _settings(),
            signature_verifier=verifier,
            session_id_factory=lambda: "opaque-session-123",
        )
        form = {"CallSid": "CA-confirm", "Direction": "outbound-api"}
        with TestClient(app) as client:
            response = client.post(
                "/twilio/voice?context_reference=confirmation-call-0001",
                data=form,
                headers={"X-Twilio-Signature": "valid-signature"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "https://voice.example.test/base/twilio/voice?context_reference=confirmation-call-0001",
            verifier.calls[0][0],
        )
        root = ElementTree.fromstring(response.text)
        parameters = [
            item.attrib for item in root.findall("./Connect/Stream/Parameter")
        ]
        self.assertIn(
            {"name": "context_reference", "value": "confirmation-call-0001"},
            parameters,
        )

    def test_requires_call_sid_after_signature_validation(self) -> None:
        app = create_voice_app(_settings(), signature_verifier=_Verifier())
        with TestClient(app) as client:
            response = client.post(
                "/twilio/voice",
                data={"Direction": "inbound"},
                headers={"X-Twilio-Signature": "valid"},
            )
        self.assertEqual(400, response.status_code)

    def test_media_endpoint_is_closed_when_runtime_not_composed(self) -> None:
        verifier = _Verifier()
        app = create_voice_app(_settings(), signature_verifier=verifier)
        with TestClient(app) as client:
            with client.websocket_connect(
                "/twilio/media",
                headers={"X-Twilio-Signature": "valid"},
            ) as websocket:
                message = websocket.receive()
        self.assertEqual("websocket.close", message["type"])
        self.assertEqual(1011, message["code"])
        self.assertEqual(
            "wss://voice.example.test/base/twilio/media",
            verifier.calls[0][0],
        )

    def test_media_endpoint_rejects_unsigned_upgrade_before_factory(self) -> None:
        factory_called = False

        def session_factory(_: object) -> _FakeMediaSession:
            nonlocal factory_called
            factory_called = True
            return _FakeMediaSession("CA-unsigned")

        app = create_voice_app(
            _settings(),
            signature_verifier=_Verifier(),
            media_session_factory=session_factory,
            actor_factory=lambda descriptor, _: _FakeActor(descriptor),  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            with self.assertRaises(WebSocketDisconnect) as rejected:
                with client.websocket_connect("/twilio/media"):
                    pass
        self.assertEqual(1008, rejected.exception.code)
        self.assertFalse(factory_called)

    def test_media_endpoint_dispatches_normalized_events_to_one_actor(self) -> None:
        sessions: list[_FakeMediaSession] = []
        actors: list[_FakeActor] = []

        def session_factory(_: object) -> _FakeMediaSession:
            session = _FakeMediaSession(
                "CA-media",
                [
                    SessionEvent("telephony.audio", {"chunk": "normalized"}),
                    SessionEvent("telephony.stop", {"reason": "provider_stop"}),
                ],
            )
            sessions.append(session)
            return session

        def actor_factory(descriptor: CallDescriptor, _: object) -> _FakeActor:
            actor = _FakeActor(descriptor)
            actors.append(actor)
            return actor

        app = create_voice_app(
            _settings(),
            signature_verifier=_Verifier(),
            media_session_factory=session_factory,
            actor_factory=actor_factory,  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            with client.websocket_connect(
                "/twilio/media",
                headers={"X-Twilio-Signature": "valid"},
            ) as websocket:
                websocket.receive()

        self.assertEqual(1, len(sessions))
        self.assertEqual(1, len(actors))
        self.assertEqual(1, actors[0].start_count)
        self.assertEqual(
            ["telephony.audio", "telephony.stop"],
            [event.kind for event in actors[0].events],
        )

    def test_protocol_error_is_reported_to_actor_before_shutdown(self) -> None:
        actors: list[_FakeActor] = []

        def actor_factory(descriptor: CallDescriptor, _: object) -> _FakeActor:
            actor = _FakeActor(descriptor)
            actors.append(actor)
            return actor

        app = create_voice_app(
            _settings(),
            signature_verifier=_Verifier(),
            media_session_factory=lambda _: _FakeMediaSession(
                "CA-invalid",
                receive_error=ValueError("malformed provider frame"),
            ),
            actor_factory=actor_factory,  # type: ignore[arg-type]
        )
        with TestClient(app) as client:
            with client.websocket_connect(
                "/twilio/media",
                headers={"X-Twilio-Signature": "valid"},
            ) as websocket:
                websocket.receive()

        self.assertEqual(["voice.error"], [event.kind for event in actors[0].events])
        self.assertEqual(
            "invalid_media_event",
            actors[0].events[0].data["reason"],
        )

    def test_rejects_partially_configured_media_dependencies(self) -> None:
        with self.assertRaisesRegex(ValueError, "configured together"):
            create_voice_app(
                _settings(),
                signature_verifier=_Verifier(),
                media_session_factory=lambda _: _FakeMediaSession("CA123"),
            )

    def test_public_websocket_url_preserves_base_path(self) -> None:
        self.assertEqual(
            "wss://voice.example.test/prefix/twilio/media",
            public_media_websocket_url("https://voice.example.test/prefix/"),
        )


class MediaSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_duplicate_connections_create_one_actor(self) -> None:
        actors: list[_FakeActor] = []

        def actor_factory(descriptor: CallDescriptor, _: object) -> _FakeActor:
            actor = _FakeActor(descriptor)
            actors.append(actor)
            return actor

        supervisor = MediaBoundCallSupervisor(actor_factory)  # type: ignore[arg-type]
        first = _FakeMediaSession("same-call")
        second = _FakeMediaSession("same-call")

        accepted = await asyncio.gather(
            supervisor.accept(first),
            supervisor.accept(second),
        )

        self.assertIs(accepted[0][0], accepted[1][0])
        self.assertEqual({True, False}, {accepted[0][1], accepted[1][1]})
        self.assertEqual(1, len(actors))
        await supervisor.close_all("test_cleanup")


if __name__ == "__main__":
    unittest.main()
