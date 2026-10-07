from __future__ import annotations

import asyncio
import base64
import json
import unittest

from appointment_harness.fixtures import load_fixture
from appointment_harness.service import AppointmentHarness
from clinic_agent.agent.prompts import PromptContext
from clinic_agent.call_mechanics import (
    AsyncWorkerPool,
    CallSessionActor,
    InMemoryEventSink,
)
from clinic_agent.call_mechanics.actor import SessionClosedError
from clinic_agent.control_plane.state_machine import ConversationStateMachine
from clinic_agent.providers.openai_live import GPTLiveVoiceEngine
from clinic_agent.providers.twilio import TwilioMediaSession
from clinic_agent.runtime.voice_control_plane import VoiceAgentControlPlane


class FakeWebSocket:
    def __init__(self, messages: list[str]) -> None:
        self.messages = asyncio.Queue()
        for message in messages:
            self.messages.put_nowait(message)
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def receive_text(self) -> str:
        return await self.messages.get()

    async def send_json(self, message: object) -> None:
        self.sent.append(dict(message))  # type: ignore[arg-type]

    async def close(self, *, code: int, reason: str) -> None:
        self.closed = True


class FakeLiveConnection:
    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.events.put_nowait({"type": "session.started", "session": {"id": "live-1"}})
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def send(self, event: object) -> None:
        self.sent.append(dict(event))  # type: ignore[arg-type]

    async def recv(self) -> dict[str, object]:
        return await self.events.get()

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed = True


async def wait_until(predicate: object) -> None:
    for _ in range(200):
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class VoiceVerticalSliceTests(unittest.IsolatedAsyncioTestCase):
    async def test_twilio_audio_round_trips_through_live_engine_and_actor(self) -> None:
        start = {
            "event": "start",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "tracks": ["inbound"],
                "customParameters": {"session_id": "opaque"},
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "channels": 1,
                },
            },
        }
        caller_audio = b"\xff\x7f"
        media = {
            "event": "media",
            "streamSid": "MZ1",
            "media": {
                "track": "inbound",
                "chunk": "1",
                "timestamp": "20",
                "payload": base64.b64encode(caller_audio).decode("ascii"),
            },
        }
        websocket = FakeWebSocket(
            [
                '{"event":"connected","protocol":"Call","version":"1.0.0"}',
                json.dumps(start),
                json.dumps(media),
            ]
        )
        telephony = await TwilioMediaSession.create(websocket)
        store, clock = load_fixture()
        harness = AppointmentHarness(store, clock)
        state = ConversationStateMachine(patient_display_name="Asha Rao")
        control = VoiceAgentControlPlane(
            harness,
            session_id="voice-CA1",
            patient_id="patient-001",
            prompt_context=PromptContext(
                assistant_name="Mira",
                clinic_name="Aster Demo Clinic",
                approved_urgent_message="Please contact emergency services now.",
                constitution_version="test",
                constitution_hash="hash",
                clinic_policy_version="test",
            ),
            state_machine=state,
        )
        live_connection = FakeLiveConnection()
        actor: CallSessionActor | None = None

        async def deliver(event: object) -> None:
            if actor is None or actor.done.is_set():
                return
            try:
                await actor.dispatch(event)  # type: ignore[arg-type]
            except SessionClosedError:
                return

        voice = GPTLiveVoiceEngine(
            deliver,
            connection_factory=lambda: live_connection,
            close_timeout=0,
        )
        actor = CallSessionActor(
            telephony.descriptor,
            telephony,
            voice,
            control,
            InMemoryEventSink(),
            AsyncWorkerPool(2),
        )
        await actor.start()

        inbound_event = await telephony.receive_event()
        assert inbound_event is not None
        await actor.dispatch(inbound_event)
        self.assertEqual("session.input_audio.append", live_connection.sent[-1]["type"])
        self.assertEqual(
            base64.b64encode(caller_audio).decode("ascii"),
            live_connection.sent[-1]["audio"],
        )

        live_connection.events.put_nowait(
            {
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(b"\x01\x02\x03\x04\x05\x06\x07\x08").decode("ascii"),
            }
        )
        await wait_until(lambda: len(websocket.sent) == 2)
        self.assertEqual(["media", "mark"], [item["event"] for item in websocket.sent])

        await actor.close("test_complete")
        self.assertTrue(websocket.closed)
        self.assertTrue(live_connection.closed)


if __name__ == "__main__":
    unittest.main()
