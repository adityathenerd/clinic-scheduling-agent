from __future__ import annotations

import asyncio
import base64
import json
import unittest
from collections.abc import Mapping
from typing import Any

from clinic_agent.call_mechanics.models import (
    AudioChunk,
    NormalizedEventKind,
    OperationKind,
    ToolResult,
    ToolStatus,
    VoiceArchitecture,
    VoiceSessionConfig,
)
from clinic_agent.providers.openai_live import GPTLiveVoiceEngine


class FakeConnection:
    def __init__(
        self,
        events: list[Mapping[str, Any]],
        *,
        auto_ack_instructions: bool = True,
    ) -> None:
        self.events: asyncio.Queue[Mapping[str, Any]] = asyncio.Queue()
        for event in events:
            self.events.put_nowait(event)
        self.sent: list[Mapping[str, Any]] = []
        self.closed: list[tuple[int, str]] = []
        self.auto_ack_instructions = auto_ack_instructions

    async def send(self, event: Mapping[str, Any]) -> None:
        self.sent.append(event)
        if (
            self.auto_ack_instructions
            and event.get("type") == "session.instructions.append"
        ):
            self.events.put_nowait(
                {
                    "type": "session.instructions.appended",
                    "client_event_id": event["event_id"],
                }
            )

    async def recv(self) -> Mapping[str, Any]:
        return await self.events.get()

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


class FakeManager:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.entered = 0

    async def enter(self) -> FakeConnection:
        self.entered += 1
        return self.connection


async def wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def session_config() -> VoiceSessionConfig:
    return VoiceSessionConfig(
        instructions="Be concise.",
        backend_instructions="Use only guarded tools.",
        initial_commentary="Hello, how can I help with scheduling?",
        tools=(
            {
                "type": "function",
                "name": "search_slots",
                "parameters": {"type": "object"},
            },
        ),
        architecture=VoiceArchitecture.LIVE_DELEGATED,
        frontend_model="gpt-live-1",
        backend_model="gpt-6-sol",
        backend_reasoning_effort="low",
    )


class GPTLiveVoiceEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.received = []

        async def receive(event: Any) -> None:
            self.received.append(event)

        self.receive = receive

    async def test_connect_sends_session_start_first_and_waits_for_started(self) -> None:
        connection = FakeConnection(
            [{"type": "session.started", "session": {"id": "sess-1"}}]
        )
        manager = FakeManager(connection)
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: manager, close_timeout=0
        )

        await engine.connect(session_config())
        await wait_until(lambda: bool(self.received))

        self.assertEqual(1, manager.entered)
        start = connection.sent[0]
        self.assertEqual("session.start", start["type"])
        session = start["session"]
        self.assertEqual("gpt-live-1", session["model"])
        self.assertEqual(
            {"type": "audio/pcmu", "rate": 8000}, session["audio"]["format"]
        )
        self.assertEqual("responses", session["delegation"]["type"])
        self.assertEqual("gpt-6-sol", session["delegation"]["responses"]["model"])
        self.assertEqual(
            "Use only guarded tools.",
            session["delegation"]["responses"]["instructions"],
        )
        self.assertFalse(session["delegation"]["responses"]["parallel_tool_calls"])
        self.assertEqual(
            {
                "type": "session.commentary.append",
                "content": "Hello, how can I help with scheduling?",
                "delegation_id": None,
            },
            connection.sent[-1],
        )
        self.assertEqual(
            NormalizedEventKind.LIVE_SESSION_STARTED.value, self.received[0].kind
        )
        await engine.close()

    async def test_push_audio_base64_encodes_raw_pcmu(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())

        await engine.push_audio(AudioChunk(b"\x00\xff", sequence=4, timestamp_ms=20))

        self.assertEqual(
            {
                "type": "session.input_audio.append",
                "audio": base64.b64encode(b"\x00\xff").decode("ascii"),
            },
            connection.sent[-1],
        )
        await engine.close()

    async def test_maps_audio_transcripts_delegation_and_unknown_events(self) -> None:
        audio = b"12345678" * 2
        connection = FakeConnection(
            [
                {"type": "session.started", "session": {"id": "sess-1"}},
                {
                    "type": "session.output_audio.delta",
                    "delta": base64.b64encode(audio).decode("ascii"),
                },
                {
                    "type": "session.input_transcript.delta",
                    "delta": "Friday",
                    "start_ms": 10,
                    "end_ms": 20,
                },
                {
                    "type": "session.output_transcript.delta",
                    "delta": "Certainly",
                    "start_ms": 20,
                    "end_ms": 30,
                },
                {
                    "type": "session.delegation.created",
                    "delegation": {"id": "del-1", "target": "responses"},
                },
                {"type": "future.live.event", "payload": "not forwarded"},
            ]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await wait_until(lambda: len(self.received) == 6)

        audio_event = self.received[1]
        self.assertEqual("voice.audio", audio_event.kind)
        self.assertEqual(audio, audio_event.data["chunk"].payload)
        self.assertEqual(2, audio_event.data["audio_end_ms"])
        self.assertEqual("voice.input_transcript.delta", self.received[2].kind)
        self.assertEqual("voice.output_transcript.delta", self.received[3].kind)
        self.assertEqual(1, self.received[3].data["audio_sequence"])
        self.assertEqual(
            NormalizedEventKind.DELEGATION_CREATED.value, self.received[4].kind
        )
        self.assertEqual("del-1", self.received[4].data["delegation_id"])
        self.assertEqual("provider.unknown_event", self.received[5].kind)
        await engine.close()

    async def test_maps_nested_response_tool_call_and_completion_lifecycle(self) -> None:
        connection = FakeConnection(
            [
                {"type": "session.started", "session": {}},
                {
                    "type": "response.event",
                    "delegation_id": "del-1",
                    "event": {"type": "response.created", "response": {"id": "resp-1"}},
                },
                {
                    "type": "response.event",
                    "delegation_id": "del-1",
                    "event": {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "create_appointment",
                            "arguments": '{"slot_id":"slot-1"}',
                        },
                    },
                },
                {
                    "type": "response.event",
                    "delegation_id": "del-1",
                    "event": {"type": "response.completed", "response": {}},
                },
            ]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await wait_until(lambda: len(self.received) == 3)

        self.assertEqual(
            NormalizedEventKind.BACKEND_RESPONSE_STARTED.value, self.received[1].kind
        )
        tool_event = self.received[2]
        self.assertEqual(NormalizedEventKind.BACKEND_TOOL_REQUESTED.value, tool_event.kind)
        intent = tool_event.data["intent"]
        self.assertEqual("call-1", intent.operation_id)
        self.assertEqual(OperationKind.WRITE, intent.operation_kind)
        self.assertEqual({"slot_id": "slot-1"}, intent.arguments)
        self.assertNotIn(
            NormalizedEventKind.DELEGATION_COMPLETED.value,
            [event.kind for event in self.received],
        )

        await engine.submit_tool_result(
            ToolResult("call-1", ToolStatus.SUCCEEDED, {"appointment_id": "apt-1"})
        )
        created, continued = connection.sent[-2:]
        self.assertEqual("response.item.create", created["type"])
        self.assertEqual("function_call_output", created["item"]["type"])
        self.assertEqual("call-1", created["item"]["call_id"])
        output = json.loads(created["item"]["output"])
        self.assertEqual("succeeded", output["status"])
        self.assertEqual({"appointment_id": "apt-1"}, output["payload"])
        self.assertEqual({"type": "response.create"}, continued)

        connection.events.put_nowait(
            {
                "type": "response.event",
                "delegation_id": "del-1",
                "event": {"type": "response.completed", "response": {}},
            }
        )
        await wait_until(
            lambda: any(
                event.kind == NormalizedEventKind.DELEGATION_COMPLETED.value
                for event in self.received
            )
        )
        await engine.close()

    async def test_documented_passive_response_events_do_not_pollute_unknown_log(self) -> None:
        connection = FakeConnection(
            [
                {"type": "session.started", "session": {}},
                {
                    "type": "response.event",
                    "delegation_id": "del-passive",
                    "event": {
                        "type": "response.output_text.delta",
                        "delta": "working",
                    },
                },
                {
                    "type": "response.event",
                    "delegation_id": "del-passive",
                    "event": {
                        "type": "response.function_call_arguments.delta",
                        "delta": "{",
                    },
                },
            ]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertNotIn(
            "provider.unknown_event", [event.kind for event in self.received]
        )
        await engine.close()

    async def test_documented_passive_session_events_do_not_pollute_unknown_log(self) -> None:
        connection = FakeConnection(
            [
                {"type": "session.started", "session": {}},
                {"type": "session.commentary.appended"},
                {"type": "session.usage.updated", "usage": {"input_tokens": 1}},
            ]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertNotIn(
            "provider.unknown_event", [event.kind for event in self.received]
        )
        await engine.close()

    async def test_tool_response_completion_cannot_finish_continuation_early(self) -> None:
        connection = FakeConnection(
            [
                {"type": "session.started", "session": {}},
                {
                    "type": "session.delegation.created",
                    "delegation": {"id": "del-race"},
                },
                {
                    "type": "response.event",
                    "delegation_id": "del-race",
                    "event": {
                        "type": "response.created",
                        "response": {"id": "resp-tool"},
                    },
                },
                {
                    "type": "response.event",
                    "delegation_id": "del-race",
                    "event": {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "call_id": "call-race",
                            "name": "search_slots",
                            "arguments": (
                                '{"appointment_type":"dermatology-followup",'
                                '"date_from":"2026-10-07","date_to":"2026-10-07"}'
                            ),
                        },
                    },
                },
            ]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await wait_until(
            lambda: any(
                event.kind == NormalizedEventKind.BACKEND_TOOL_REQUESTED.value
                for event in self.received
            )
        )

        await engine.submit_tool_result(
            ToolResult("call-race", ToolStatus.SUCCEEDED, {"slots": []})
        )
        connection.events.put_nowait(
            {
                "type": "response.event",
                "delegation_id": "del-race",
                "event": {
                    "type": "response.completed",
                    "response": {"id": "resp-tool"},
                },
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertNotIn(
            NormalizedEventKind.DELEGATION_COMPLETED.value,
            [event.kind for event in self.received],
        )

        connection.events.put_nowait(
            {
                "type": "response.event",
                "delegation_id": "del-race",
                "event": {
                    "type": "response.created",
                    "response": {"id": "resp-continuation"},
                },
            }
        )
        connection.events.put_nowait(
            {
                "type": "response.event",
                "delegation_id": "del-race",
                "event": {
                    "type": "response.completed",
                    "response": {"id": "resp-continuation"},
                },
            }
        )
        await wait_until(
            lambda: any(
                event.kind == NormalizedEventKind.DELEGATION_COMPLETED.value
                for event in self.received
            )
        )
        await engine.close()

    async def test_cancelled_delegation_drops_late_response_events(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await engine.cancel_delegation("del-stale", "caller correction")
        connection.events.put_nowait(
            {
                "type": "response.event",
                "delegation_id": "del-stale",
                "event": {"type": "response.created"},
            }
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertNotIn(
            NormalizedEventKind.BACKEND_RESPONSE_STARTED.value,
            [event.kind for event in self.received],
        )
        await engine.close()

    async def test_cancel_and_truncate_do_not_invent_live_wire_commands(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        sent_before = len(connection.sent)

        await engine.cancel_response()
        await engine.truncate_response("gpt-live-output", 120)

        self.assertEqual(sent_before, len(connection.sent))
        await engine.close()

    async def test_application_commentary_is_sent_as_supported_live_event(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await engine.append_commentary("Please contact emergency services now.")
        self.assertEqual(
            {
                "type": "session.commentary.append",
                "content": "Please contact emergency services now.",
                "delegation_id": None,
            },
            connection.sent[-1],
        )
        await engine.close()

    async def test_application_thinking_is_sent_as_quiet_live_context(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await engine.append_thinking(
            "The application told the caller that identity is confirmed."
        )
        self.assertEqual(
            {
                "type": "session.thinking.append",
                "content": (
                    "The application told the caller that identity is confirmed."
                ),
                "delegation_id": None,
            },
            connection.sent[-1],
        )
        await engine.close()

    async def test_exact_application_wording_uses_live_instruction_event(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        authorized_sequence = await engine.append_instructions(
            "Say exactly: Thank you. Take care."
        )
        instruction = connection.sent[-1]
        self.assertEqual("session.instructions.append", instruction["type"])
        self.assertEqual(
            "Say exactly: Thank you. Take care.", instruction["content"]
        )
        self.assertEqual(None, instruction["delegation_id"])
        self.assertTrue(str(instruction["event_id"]).startswith("instruction-"))
        self.assertEqual(0, authorized_sequence)
        await engine.close()

    async def test_exact_instruction_waits_for_matching_acknowledgement(self) -> None:
        connection = FakeConnection(
            [{"type": "session.started", "session": {}}],
            auto_ack_instructions=False,
        )
        engine = GPTLiveVoiceEngine(
            self.receive,
            connection_factory=lambda: connection,
            close_timeout=0,
            instruction_ack_timeout=0.2,
        )
        await engine.connect(session_config())

        pending = asyncio.create_task(engine.append_instructions("Say exactly: Ready."))
        await wait_until(
            lambda: any(
                event.get("type") == "session.instructions.append"
                for event in connection.sent
            )
        )
        self.assertFalse(pending.done())
        instruction = connection.sent[-1]
        connection.events.put_nowait(
            {
                "type": "session.instructions.appended",
                "client_event_id": instruction["event_id"],
            }
        )

        self.assertEqual(0, await pending)
        await engine.close()

    async def test_pre_ack_transcript_has_strictly_older_output_sequence(self) -> None:
        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(self.receive, connection_factory=lambda: connection, close_timeout=0)
        await engine.connect(session_config())
        connection.events.put_nowait({"type": "session.output_transcript.delta", "delta": "Okay, let's set that up."})
        await wait_until(lambda: any(e.kind == "voice.output_transcript.delta" for e in self.received))
        cutoff = await engine.append_instructions("Say exactly: Exact proposal.")
        transcript = next(e for e in self.received if e.kind == "voice.output_transcript.delta")
        self.assertLess(transcript.data["audio_sequence"], cutoff)
        await engine.close()

    async def test_new_instructed_output_before_ack_is_inside_authorized_timeline(self) -> None:
        connection = FakeConnection(
            [{"type": "session.started", "session": {}}],
            auto_ack_instructions=False,
        )
        engine = GPTLiveVoiceEngine(
            self.receive,
            connection_factory=lambda: connection,
            close_timeout=0,
            instruction_ack_timeout=0.2,
        )
        await engine.connect(session_config())

        pending = asyncio.create_task(
            engine.append_instructions("Say exactly: Please confirm this request.")
        )
        await wait_until(
            lambda: any(e.get("type") == "session.instructions.append" for e in connection.sent)
        )
        instruction = connection.sent[-1]
        connection.events.put_nowait(
            {"type": "session.output_transcript.delta", "delta": "Please"}
        )
        await wait_until(lambda: any(e.kind == "voice.output_transcript.delta" for e in self.received))
        transcript = next(e for e in self.received if e.kind == "voice.output_transcript.delta")
        connection.events.put_nowait(
            {
                "type": "session.instructions.appended",
                "client_event_id": instruction["event_id"],
            }
        )

        cutoff = await pending
        self.assertGreaterEqual(transcript.data["audio_sequence"], cutoff)
        await engine.close()

    async def test_instruction_ack_is_not_blocked_by_slow_application_callback(self) -> None:
        callback_started = asyncio.Event()
        release_callback = asyncio.Event()

        async def blocking_receive(event: Any) -> None:
            if event.kind == "voice.audio":
                callback_started.set()
                await release_callback.wait()

        connection = FakeConnection([{"type": "session.started", "session": {}}])
        engine = GPTLiveVoiceEngine(
            blocking_receive,
            connection_factory=lambda: connection,
            close_timeout=0,
            instruction_ack_timeout=0.2,
        )
        await engine.connect(session_config())
        connection.events.put_nowait(
            {
                "type": "session.output_audio.delta",
                "delta": base64.b64encode(b"12345678").decode("ascii"),
            }
        )
        await asyncio.wait_for(callback_started.wait(), timeout=0.2)

        authorized_sequence = await engine.append_instructions(
            "Say exactly: Identity confirmed."
        )

        self.assertEqual(1, authorized_sequence)
        release_callback.set()
        await engine.close()

    async def test_exact_instruction_fails_closed_when_acknowledgement_times_out(self) -> None:
        connection = FakeConnection(
            [{"type": "session.started", "session": {}}],
            auto_ack_instructions=False,
        )
        engine = GPTLiveVoiceEngine(
            self.receive,
            connection_factory=lambda: connection,
            close_timeout=0,
            instruction_ack_timeout=0.01,
        )
        await engine.connect(session_config())

        with self.assertRaisesRegex(TimeoutError, "did not acknowledge"):
            await engine.append_instructions("Say exactly: Ready.")

        await engine.close()

    async def test_error_and_session_close_are_normalized(self) -> None:
        connection = FakeConnection(
            [
                {"type": "session.started", "session": {}},
                {"type": "error", "error": {"code": "bad_event", "message": "bad"}},
                {"type": "session.closed", "reason": "remote_hangup"},
            ]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        await engine.connect(session_config())
        await wait_until(lambda: len(self.received) == 3)
        self.assertEqual("voice.error", self.received[1].kind)
        self.assertEqual("bad", self.received[1].data["reason"])
        self.assertEqual(
            NormalizedEventKind.LIVE_SESSION_CLOSED.value, self.received[2].kind
        )
        self.assertEqual("remote_hangup", self.received[2].data["reason"])
        await engine.close()

    async def test_startup_error_closes_transport_and_fails_connect(self) -> None:
        connection = FakeConnection(
            [{"type": "error", "error": {"message": "admission denied"}}]
        )
        engine = GPTLiveVoiceEngine(
            self.receive, connection_factory=lambda: connection, close_timeout=0
        )
        with self.assertRaisesRegex(RuntimeError, "admission denied"):
            await engine.connect(session_config())
        self.assertEqual([(1000, "startup_failed")], connection.closed)

    async def test_rejects_direct_realtime_configuration(self) -> None:
        connection = FakeConnection([])
        config = VoiceSessionConfig(
            instructions="test", architecture=VoiceArchitecture.REALTIME_DIRECT
        )
        engine = GPTLiveVoiceEngine(self.receive, connection_factory=lambda: connection)
        with self.assertRaisesRegex(ValueError, "live_delegated"):
            await engine.connect(config)
        self.assertEqual([], connection.sent)


if __name__ == "__main__":
    unittest.main()
