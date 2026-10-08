from __future__ import annotations

import asyncio
import unittest

from clinic_agent.call_mechanics import (
    AsyncWorkerPool,
    AudioChunk,
    CallDescriptor,
    CallDirection,
    CallSessionActor,
    InMemoryEventSink,
    NormalizedEventKind,
    OperationKind,
    SessionEvent,
    SessionState,
    SpeechClip,
    ToolIntent,
    ToolResult,
    ToolStatus,
)

from .fakes import (
    ExplodingSink,
    FakeControlPlane,
    FakeSpeechRenderer,
    FakeTelephony,
    FakeVoice,
)


class ActorTests(unittest.IsolatedAsyncioTestCase):
    def build_actor(
        self,
        *,
        direction: CallDirection = CallDirection.INBOUND,
        voice: FakeVoice | None = None,
        control: FakeControlPlane | None = None,
        sink: InMemoryEventSink | ExplodingSink | None = None,
        call_id: str = "call-1",
        destination: str | None = None,
        fallback: str | None = "front-desk",
        pool: AsyncWorkerPool | None = None,
        turn_completion_delay: float = 0.75,
        speech_renderer: FakeSpeechRenderer | None = None,
        application_playback_slack: float = 5.0,
    ) -> tuple[CallSessionActor, FakeTelephony, FakeVoice, FakeControlPlane, object]:
        telephony = FakeTelephony()
        voice = voice or FakeVoice()
        control = control or FakeControlPlane()
        sink = sink or InMemoryEventSink()
        actor = CallSessionActor(
            CallDescriptor(call_id, direction, destination=destination, fallback_destination=fallback),
            telephony,
            voice,
            control,
            sink,
            pool or AsyncWorkerPool(4),
            turn_completion_delay=turn_completion_delay,
            speech_renderer=speech_renderer,
            application_playback_slack=application_playback_slack,
        )
        return actor, telephony, voice, control, sink

    async def test_inbound_bootstrap_and_audio_forwarding(self) -> None:
        actor, telephony, voice, control, _ = self.build_actor()
        await actor.start()
        chunk = AudioChunk(b"caller", 1, 20)
        await actor.dispatch(SessionEvent("telephony.audio", {"chunk": chunk}))
        self.assertEqual(SessionState.ACTIVE, actor.state)
        self.assertEqual(CallDirection.INBOUND, control.bootstrap_calls[0].direction)
        self.assertEqual([chunk], voice.pushed_audio)
        await actor.close()

    async def test_input_transcript_delta_is_observed_by_control_plane(self) -> None:
        actor, _, _, control, _ = self.build_actor()
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "voice.input_transcript.delta",
                {"delta": "Yes, this is Asha Rao"},
            )
        )
        self.assertEqual(["Yes, this is Asha Rao"], control.transcript_deltas)
        await actor.close()

    async def test_confirmation_proposal_is_application_owned_and_armed_after_append(self) -> None:
        async def handler(intent: ToolIntent) -> ToolResult:
            return ToolResult(
                intent.operation_id,
                ToolStatus.SUCCEEDED,
                payload={
                    "result": {
                        "status": "confirmation_required",
                        "exact_proposal": "Move the visit to Wednesday at 11 AM",
                    },
                    "application_directive": (
                        "say_exactly: Here is the exact proposed rescheduling: Move "
                        "the visit to Wednesday at 11 AM. Do you explicitly confirm "
                        "this rescheduling? Please say yes or no."
                    ),
                },
            )

        control = FakeControlPlane(handler=handler)
        pool = AsyncWorkerPool(4)
        actor, _, voice, _, sink = self.build_actor(
            control=control,
            pool=pool,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "proposal-delegation"},
            )
        )
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.BACKEND_TOOL_REQUESTED.value,
                {
                    "intent": ToolIntent(
                        "proposal-op",
                        "edit_appointment",
                        OperationKind.WRITE,
                        delegation_id="proposal-delegation",
                    )
                },
            )
        )
        await pool.wait_idle()
        await asyncio.sleep(0.05)

        self.assertEqual([1], control.armed_confirmation_epochs)
        self.assertEqual(1, len(voice.instructions))
        self.assertIn("exact proposed rescheduling", voice.instructions[0])
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn("confirmation.proposal_directive_appended", event_types)
        await actor.close()

    async def test_speech_stop_completes_application_owned_turn_as_trusted_instruction(self) -> None:
        control = FakeControlPlane(completed_turn_directive="Identity accepted.")
        pool = AsyncWorkerPool(4)
        actor, _, voice, _, sink = self.build_actor(
            control=control,
            turn_completion_delay=0,
            pool=pool,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent("voice.input_transcript.delta", {"delta": "Yes, speaking"})
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await asyncio.sleep(0.05)
        await pool.wait_idle()
        await asyncio.sleep(0.01)

        self.assertEqual(1, control.completed_turn_count)
        self.assertEqual([], voice.commentary)
        self.assertEqual(1, len(voice.instructions))
        self.assertIn("Identity accepted.", voice.instructions[0])
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn("caller.turn_boundary_detected", event_types)
        self.assertIn("control.completed_turn_directive", event_types)
        self.assertIn("caller.turn", event_types)
        await actor.close()

    async def test_application_owned_exact_speech_completes_only_after_twilio_mark(self) -> None:
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Identity accepted.",
            application_owns_completed_turn=True,
        )
        renderer = FakeSpeechRenderer(clip=SpeechClip(b"\xff" * 1600, 200))
        pool = AsyncWorkerPool(4)
        actor, telephony, voice, _, sink = self.build_actor(
            control=control,
            pool=pool,
            turn_completion_delay=0,
            speech_renderer=renderer,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent("voice.input_transcript.delta", {"delta": "Yes, speaking"})
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        for _ in range(100):
            if telephony.sent_audio:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(["Identity accepted."], renderer.texts)
        self.assertEqual([], voice.instructions)
        self.assertEqual(1, len(voice.thinking))
        self.assertIn("Identity accepted.", voice.thinking[0])
        self.assertEqual(2, len(telephony.sent_audio))
        self.assertFalse(
            any(
                event["event_type"] == "assistant.application_audio_completed"
                for event in sink.events  # type: ignore[attr-defined]
            )
        )

        await actor.dispatch(
            SessionEvent(
                "playback.marked",
                {"item_id": "application-speech-1", "audio_end_ms": 200},
            )
        )
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn("assistant.application_audio_completed", event_types)
        completed_turn = next(
            event
            for event in sink.events  # type: ignore[attr-defined]
            if event["event_type"] == "assistant.turn"
            and event.get("reason") == "application_playback_completed"
        )
        self.assertEqual("[REDACTED]", completed_turn["transcript"])
        self.assertEqual(len("Identity accepted."), completed_turn["transcript_characters"])

        stale = AudioChunk(b"stale", 50, 20)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {"chunk": stale, "item_id": "gpt-live-output", "audio_end_ms": 20},
            )
        )
        self.assertEqual(2, len(telephony.sent_audio))
        control._application_owns_completed_turn = False  # identity gate has completed
        await actor.dispatch(SessionEvent("caller.speech_started"))
        natural = AudioChunk(b"natural", 51, 40)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {"chunk": natural, "item_id": "gpt-live-output", "audio_end_ms": 40},
            )
        )
        self.assertEqual(natural, telephony.sent_audio[-1])
        self.assertIn(
            "control.conversation_reopened",
            [event["event_type"] for event in sink.events],  # type: ignore[attr-defined]
        )
        await actor.close()

    async def test_caller_barge_in_cancels_application_owned_playback(self) -> None:
        renderer = FakeSpeechRenderer(clip=SpeechClip(b"\xff" * 1600, 200))
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Please listen.",
            application_owns_completed_turn=True,
        )
        actor, telephony, _, _, sink = self.build_actor(
            control=control,
            turn_completion_delay=0,
            speech_renderer=renderer,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        for _ in range(100):
            if telephony.sent_audio:
                break
            await asyncio.sleep(0.01)
        clears_before = telephony.clear_count

        await actor.dispatch(SessionEvent("caller.speech_started"))

        self.assertGreater(telephony.clear_count, clears_before)
        self.assertIn(
            "assistant.application_audio_interrupted",
            [event["event_type"] for event in sink.events],  # type: ignore[attr-defined]
        )
        await actor.close()

    async def test_application_speech_render_failure_fails_closed(self) -> None:
        renderer = FakeSpeechRenderer(error=TimeoutError("synthetic TTS timeout"))
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Identity accepted.",
            application_owns_completed_turn=True,
        )
        actor, telephony, _, _, sink = self.build_actor(
            control=control,
            turn_completion_delay=0,
            speech_renderer=renderer,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await asyncio.wait_for(actor.done.wait(), timeout=1)

        self.assertEqual(SessionState.FAILED, actor.state)
        self.assertEqual(["front-desk"], telephony.transfers)
        self.assertIn(
            "assistant.application_audio_render_failed",
            [event["event_type"] for event in sink.events],  # type: ignore[attr-defined]
        )

    async def test_missing_final_playback_mark_fails_closed(self) -> None:
        renderer = FakeSpeechRenderer(clip=SpeechClip(b"\xff" * 8, 1))
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Identity accepted.",
            application_owns_completed_turn=True,
        )
        actor, telephony, _, _, sink = self.build_actor(
            control=control,
            turn_completion_delay=0,
            speech_renderer=renderer,
            application_playback_slack=0.01,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await asyncio.wait_for(actor.done.wait(), timeout=1)

        self.assertEqual(SessionState.FAILED, actor.state)
        self.assertEqual(["front-desk"], telephony.transfers)
        self.assertIn(
            "assistant.application_audio_timeout",
            [event["event_type"] for event in sink.events],  # type: ignore[attr-defined]
        )

    async def test_application_gate_quarantines_delegation_until_classification_finishes(self) -> None:
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Identity accepted.",
            application_owns_completed_turn=True,
        )
        actor, _, voice, _, sink = self.build_actor(
            control=control,
            turn_completion_delay=0.05,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent("voice.input_transcript.delta", {"delta": "Yes, speaking"})
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "gate-race"},
            )
        )

        self.assertEqual([], voice.cancelled_delegations)
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn("delegation.quarantined", event_types)
        await actor.close()

    async def test_accepted_gated_faq_releases_one_quarantined_tool_and_tracks_liveness(self) -> None:
        class GateThenActive(FakeControlPlane):
            async def complete_patient_turn(self, *, turn_epoch=None):
                self.completed_turn_count += 1
                self.completed_turn_epochs.append(turn_epoch)
                self._application_owns_completed_turn = False
                return (
                    "Application event: the caller stated another supported clinic "
                    "information request. Handle the full request now."
                )

        control = GateThenActive(application_owns_completed_turn=True)
        pool = AsyncWorkerPool(4)
        actor, _, voice, _, sink = self.build_actor(
            control=control,
            pool=pool,
            turn_completion_delay=0.05,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent(
                "voice.input_transcript.delta",
                {"delta": "Can I get prescribed medicines at the clinic pharmacy?"},
            )
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "faq-during-gate"},
            )
        )
        accepted = await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.BACKEND_TOOL_REQUESTED.value,
                {
                    "intent": ToolIntent(
                        "faq-once",
                        "search_clinic_faqs",
                        OperationKind.READ,
                        {"query": "clinic pharmacy medicines"},
                        delegation_id="faq-during-gate",
                    )
                },
            )
        )
        self.assertTrue(accepted)
        self.assertEqual([], control.tool_calls)

        for _ in range(100):
            if control.tool_calls:
                break
            await asyncio.sleep(0.005)
        await pool.wait_idle()

        self.assertEqual(["faq-once"], [intent.operation_id for intent in control.tool_calls])
        self.assertEqual([], voice.cancelled_delegations)
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn("control.normal_turn_adopted_after_gate", event_types)
        self.assertIn("delegation.released", event_types)
        self.assertIn("control.normal_turn_progress", event_types)
        self.assertNotIn("control.normal_turn_liveness_failed", event_types)
        await actor.close()

    async def test_application_gate_suppresses_stale_audio_until_directive_is_ready(self) -> None:
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Identity accepted.",
            application_owns_completed_turn=True,
        )
        voice = FakeVoice(instruction_sequence=2)
        pool = AsyncWorkerPool(4)
        actor, telephony, _, _, sink = self.build_actor(
            control=control,
            voice=voice,
            turn_completion_delay=0.05,
            pool=pool,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent("voice.input_transcript.delta", {"delta": "Yes, speaking"})
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))

        stale = AudioChunk(b"stale preamble", 1, 20)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {"chunk": stale, "item_id": "stale", "audio_end_ms": 20},
            )
        )
        self.assertEqual([], telephony.sent_audio)

        for _ in range(100):
            if voice.instructions:
                break
            await asyncio.sleep(0.01)
        await pool.wait_idle()
        self.assertTrue(voice.instructions)
        delayed_stale = AudioChunk(b"delayed stale preamble", 1, 30)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {
                    "chunk": delayed_stale,
                    "item_id": "stale",
                    "audio_end_ms": 30,
                },
            )
        )
        authorized = AudioChunk(b"authorized reply", 2, 40)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {"chunk": authorized, "item_id": "authorized", "audio_end_ms": 40},
            )
        )

        self.assertEqual([authorized], telephony.sent_audio)
        self.assertIn(
            "assistant.audio_suppressed",
            [event["event_type"] for event in sink.events],  # type: ignore[attr-defined]
        )
        suppression_reasons = {
            event.get("reason")
            for event in sink.events  # type: ignore[attr-defined]
            if event["event_type"] == "assistant.audio_suppressed"
        }
        self.assertEqual(
            {"application_gate_pending", "pre_instruction_timeline"},
            suppression_reasons,
        )
        await actor.close()

    async def test_application_gate_suppresses_stale_transcript_before_authorized_sequence(self) -> None:
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Approved delivery sentence.",
            application_owns_completed_turn=True,
        )
        voice = FakeVoice(instruction_sequence=2)
        pool = AsyncWorkerPool(4)
        actor, _, _, _, sink = self.build_actor(
            control=control,
            voice=voice,
            turn_completion_delay=0,
            pool=pool,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent("voice.input_transcript.delta", {"delta": "Yes"})
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        for _ in range(100):
            if voice.instructions:
                break
            await asyncio.sleep(0.01)
        await actor.dispatch(
            SessionEvent(
                "voice.output_transcript.delta",
                {"delta": "Stale autonomous preamble. ", "audio_sequence": 1},
            )
        )
        await actor.dispatch(
            SessionEvent(
                "voice.output_transcript.delta",
                {"delta": "Approved delivery sentence.", "audio_sequence": 2},
            )
        )
        await actor.dispatch(SessionEvent("caller.speech_started"))

        assistant_turns = [
            event
            for event in sink.events  # type: ignore[attr-defined]
            if event["event_type"] == "assistant.turn"
        ]
        self.assertEqual("[REDACTED]", assistant_turns[-1]["transcript"])
        self.assertEqual(
            len("Approved delivery sentence."),
            assistant_turns[-1]["transcript_characters"],
        )
        self.assertTrue(
            any(
                event["event_type"] == "assistant.transcript_suppressed"
                for event in sink.events  # type: ignore[attr-defined]
            )
        )
        await actor.close()

    async def test_identity_gate_plays_one_progress_update_before_final_directive(self) -> None:
        progress = "Thank you. I'm checking your identity confirmation now."
        control = FakeControlPlane(
            completed_turn_directive=(
                "say_exactly: Thank you for confirming. What appointment matter "
                "would you like help with?"
            ),
            application_owns_completed_turn=True,
            application_gate_progress_message=progress,
        )
        voice = FakeVoice(instruction_sequence=0)
        pool = AsyncWorkerPool(4)
        actor, telephony, _, _, sink = self.build_actor(
            control=control,
            voice=voice,
            turn_completion_delay=0.05,
            pool=pool,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent(
                "voice.input_transcript.delta",
                {"delta": "Yes, this is Asha Rao speaking"},
            )
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))

        self.assertEqual([], voice.commentary)
        self.assertEqual(1, len(voice.instructions))
        self.assertIn(progress, voice.instructions[0])
        progress_audio = AudioChunk(b"checking identity", 0, 20)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {
                    "chunk": progress_audio,
                    "item_id": "progress",
                    "audio_end_ms": 20,
                },
            )
        )
        self.assertEqual([progress_audio], telephony.sent_audio)
        voice.instruction_sequence = 1

        await asyncio.sleep(0.08)
        await pool.wait_idle()
        await asyncio.sleep(0.01)
        authorized = AudioChunk(b"identity confirmed", 1, 40)
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {
                    "chunk": authorized,
                    "item_id": "confirmed",
                    "audio_end_ms": 40,
                },
            )
        )

        self.assertEqual([progress_audio, authorized], telephony.sent_audio)
        event_types = [
            event["event_type"] for event in sink.events  # type: ignore[attr-defined]
        ]
        self.assertIn("control.application_gate_progress_appended", event_types)
        self.assertIn("assistant.gate_progress_audio_started", event_types)
        self.assertIn("assistant.authoritative_audio_started", event_types)
        await actor.close()

    async def test_say_exactly_completed_turn_uses_instructions_without_commentary(self) -> None:
        control = FakeControlPlane(
            completed_turn_directive="say_exactly: Thank you. Take care."
        )
        pool = AsyncWorkerPool(4)
        actor, _, voice, _, sink = self.build_actor(
            control=control,
            turn_completion_delay=0,
            pool=pool,
        )
        await actor.start()
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(
            SessionEvent("voice.input_transcript.delta", {"delta": "No, thanks"})
        )
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await asyncio.sleep(0.05)
        await pool.wait_idle()
        await asyncio.sleep(0.01)

        self.assertEqual([], voice.commentary)
        self.assertEqual(1, len(voice.instructions))
        self.assertIn("Thank you. Take care.", voice.instructions[0])
        event = [
            item
            for item in sink.events  # type: ignore[attr-defined]
            if item["event_type"] == "control.completed_turn_directive"
        ][-1]
        self.assertEqual("instructions", event["delivery_mode"])
        await actor.close()

    async def test_assistant_transcript_is_preserved_as_one_auditable_turn(self) -> None:
        actor, _, _, _, sink = self.build_actor()
        await actor.start()
        await actor.dispatch(
            SessionEvent("voice.output_transcript.delta", {"delta": "Your appointment "})
        )
        await actor.dispatch(
            SessionEvent("voice.output_transcript.delta", {"delta": "is at nine."})
        )
        await actor.dispatch(SessionEvent("caller.speech_started"))

        turns = [
            event
            for event in sink.events  # type: ignore[attr-defined]
            if event["event_type"] == "assistant.turn"
        ]
        self.assertEqual(1, len(turns))
        self.assertEqual(28, turns[0]["transcript_characters"])
        self.assertEqual("[REDACTED]", turns[0]["transcript"])
        await actor.close()

    async def test_provider_unknown_event_retains_provider_type(self) -> None:
        actor, _, _, _, sink = self.build_actor()
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "provider.unknown_event",
                {"provider_type": "response.event:future"},
            )
        )
        event = [
            item
            for item in sink.events  # type: ignore[attr-defined]
            if item["event_type"] == "provider.unknown_event"
        ][-1]
        self.assertEqual("response.event:future", event["provider_type"])
        await actor.close()

    async def test_immediate_control_directive_is_appended_to_live_session(self) -> None:
        control = FakeControlPlane(
            transcript_directive="Please contact emergency services now."
        )
        actor, _, voice, _, sink = self.build_actor(control=control)
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "voice.input_transcript.delta",
                {"delta": "I cannot breathe"},
            )
        )
        self.assertEqual([], voice.commentary)
        self.assertEqual(1, len(voice.instructions))
        self.assertIn("Please contact emergency services now.", voice.instructions[0])
        self.assertIn(
            "control.directive_appended",
            [event["event_type"] for event in sink.events],  # type: ignore[attr-defined]
        )
        await actor.close()

    async def test_media_gap_is_observed_and_duplicate_chunk_is_not_forwarded(self) -> None:
        actor, _, voice, _, sink = self.build_actor()
        await actor.start()
        first = AudioChunk(b"first", 1, 20)
        gap = AudioChunk(b"gap", 3, 60)
        duplicate = AudioChunk(b"duplicate", 3, 60)
        await actor.dispatch(SessionEvent("telephony.audio", {"chunk": first}))
        await actor.dispatch(SessionEvent("telephony.audio", {"chunk": gap}))
        await actor.dispatch(SessionEvent("telephony.audio", {"chunk": duplicate}))
        self.assertEqual([first, gap], voice.pushed_audio)
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn("media.sequence_gap", event_types)
        self.assertIn("media.duplicate_or_out_of_order", event_types)
        await actor.close()

    async def test_outbound_direction_reaches_bootstrap_without_changing_actor(self) -> None:
        actor, _, _, control, _ = self.build_actor(
            direction=CallDirection.OUTBOUND,
            destination="synthetic-recipient",
        )
        await actor.start()
        self.assertEqual(CallDirection.OUTBOUND, control.bootstrap_calls[0].direction)
        await actor.close()

    async def test_voice_connect_retries_once(self) -> None:
        actor, _, voice, _, _ = self.build_actor(voice=FakeVoice(connect_failures=1))
        await actor.start()
        self.assertEqual(2, voice.connect_calls)
        self.assertEqual(SessionState.ACTIVE, actor.state)
        await actor.close()

    async def test_voice_connect_exhaustion_transfers_and_closes_once(self) -> None:
        actor, telephony, voice, _, _ = self.build_actor(voice=FakeVoice(connect_failures=2))
        with self.assertRaises(ConnectionError):
            await actor.start()
        await actor.done.wait()
        self.assertEqual(SessionState.FAILED, actor.state)
        self.assertEqual(["front-desk"], telephony.transfers)
        self.assertEqual(1, len(telephony.close_reasons))
        self.assertEqual(1, voice.close_count)

    async def test_barge_in_clears_cancels_and_truncates_to_played_audio(self) -> None:
        actor, telephony, voice, _, _ = self.build_actor()
        await actor.start()
        chunk = AudioChunk(b"assistant", 1, 100)
        await actor.dispatch(
            SessionEvent("voice.audio", {"chunk": chunk, "item_id": "item-1", "audio_end_ms": 900})
        )
        await actor.dispatch(
            SessionEvent("playback.marked", {"item_id": "item-1", "audio_end_ms": 420})
        )
        await actor.dispatch(SessionEvent("caller.speech_started"))
        self.assertEqual(1, telephony.clear_count)
        self.assertEqual(1, voice.cancel_count)
        self.assertEqual([("item-1", 420)], voice.truncations)
        await actor.close()

    async def test_repeated_speech_start_does_not_repeat_interruption(self) -> None:
        actor, telephony, voice, _, _ = self.build_actor()
        await actor.start()
        chunk = AudioChunk(b"assistant", 1, 100)
        await actor.dispatch(
            SessionEvent("voice.audio", {"chunk": chunk, "item_id": "item-1", "audio_end_ms": 900})
        )
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("caller.speech_started"))
        self.assertEqual(1, telephony.clear_count)
        self.assertEqual(1, voice.cancel_count)
        await actor.close()

    async def test_barge_in_stops_playback_without_cancelling_backend_work(self) -> None:
        actor, _, voice, _, _ = self.build_actor()
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(
            SessionEvent(
                "voice.audio",
                {
                    "chunk": AudioChunk(b"assistant", 1, 100),
                    "item_id": "item-1",
                    "audio_end_ms": 900,
                },
            )
        )
        await actor.dispatch(SessionEvent("caller.speech_started"))
        self.assertEqual(1, voice.cancel_count)
        self.assertEqual([], voice.cancelled_delegations)
        await actor.close()

    async def test_correction_cancels_and_invalidates_active_delegation(self) -> None:
        actor, _, voice, _, sink = self.build_actor()
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(SessionEvent("caller.correction"))
        accepted = await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_COMPLETED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        self.assertFalse(accepted)
        self.assertEqual(
            [("delegation-1", "caller.correction")],
            voice.cancelled_delegations,
        )
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        self.assertIn(NormalizedEventKind.DELEGATION_CANCELLED.value, event_types)
        self.assertIn(NormalizedEventKind.DELEGATION_STALE.value, event_types)
        await actor.close()

    async def test_stale_backend_tool_request_is_rejected_before_execution(self) -> None:
        actor, _, _, control, _ = self.build_actor()
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(SessionEvent("escalation.requested"))
        accepted = await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.BACKEND_TOOL_REQUESTED.value,
                {
                    "intent": ToolIntent(
                        "op-1",
                        "search_slots",
                        OperationKind.READ,
                        delegation_id="delegation-1",
                    )
                },
            )
        )
        self.assertFalse(accepted)
        self.assertEqual([], control.tool_calls)
        await actor.close()

    async def test_inflight_stale_read_finishes_but_result_is_not_spoken(self) -> None:
        release = asyncio.Event()

        async def slow_read(intent: ToolIntent) -> ToolResult:
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED)

        actor, _, voice, control, sink = self.build_actor(control=FakeControlPlane(slow_read))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.BACKEND_TOOL_REQUESTED.value,
                {
                    "intent": ToolIntent(
                        "op-1",
                        "search_slots",
                        OperationKind.READ,
                        delegation_id="delegation-1",
                    )
                },
            )
        )
        await actor.dispatch(SessionEvent("caller.correction"))
        release.set()
        await actor.wait_idle()
        self.assertEqual(1, len(control.tool_calls))
        self.assertEqual([], voice.tool_results)
        completions = [
            event
            for event in sink.events  # type: ignore[attr-defined]
            if event["event_type"] == NormalizedEventKind.BACKEND_TOOL_COMPLETED.value
        ]
        self.assertEqual(True, completions[0]["stale"])
        await actor.close()

    async def test_live_stage_events_are_normalized_for_trace_attribution(self) -> None:
        actor, _, _, _, sink = self.build_actor()
        await actor.start()
        await actor.dispatch(SessionEvent(NormalizedEventKind.LIVE_SESSION_STARTED.value))
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_CREATED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.BACKEND_RESPONSE_STARTED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.LIVE_COMMENTARY_APPENDED.value,
                {"delegation_id": "delegation-1", "character_count": 12},
            )
        )
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.DELEGATION_COMPLETED.value,
                {"delegation_id": "delegation-1"},
            )
        )
        await actor.dispatch(
            SessionEvent(
                NormalizedEventKind.LIVE_SESSION_CLOSED.value,
                {"reason": "normal"},
            )
        )
        event_types = [event["event_type"] for event in sink.events]  # type: ignore[attr-defined]
        for expected in {
            NormalizedEventKind.LIVE_SESSION_STARTED.value,
            NormalizedEventKind.DELEGATION_CREATED.value,
            NormalizedEventKind.BACKEND_RESPONSE_STARTED.value,
            NormalizedEventKind.LIVE_COMMENTARY_APPENDED.value,
            NormalizedEventKind.DELEGATION_COMPLETED.value,
            NormalizedEventKind.LIVE_SESSION_CLOSED.value,
        }:
            self.assertIn(expected, event_types)
        await actor.close()

    async def test_unknown_provider_event_is_observed_not_fatal(self) -> None:
        actor, _, _, _, sink = self.build_actor()
        await actor.start()
        await actor.dispatch(SessionEvent("future.provider.event", {"payload": "secret"}))
        self.assertEqual(SessionState.ACTIVE, actor.state)
        self.assertTrue(
            any(event["event_type"] == "provider.unknown_event" for event in sink.events)  # type: ignore[attr-defined]
        )
        await actor.close()

    async def test_telemetry_failure_does_not_break_call(self) -> None:
        actor, _, _, _, _ = self.build_actor(sink=ExplodingSink())
        await actor.start()
        self.assertEqual(SessionState.ACTIVE, actor.state)
        await actor.close()

    async def test_duplicate_tool_operation_is_executed_once(self) -> None:
        actor, _, _, control, sink = self.build_actor()
        await actor.start()
        intent = ToolIntent(
            "op-1",
            "search_slots",
            OperationKind.READ,
            {"date_from": "2026-10-12", "patient_id": "must-redact"},
        )
        first = await actor.dispatch(SessionEvent("tool.requested", {"intent": intent}))
        second = await actor.dispatch(SessionEvent("tool.requested", {"intent": intent}))
        await actor.wait_idle()
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(1, len(control.tool_calls))
        requested = next(
            event
            for event in sink.events  # type: ignore[attr-defined]
            if event["event_type"] == "tool.requested"
        )
        self.assertEqual("2026-10-12", requested["arguments"]["date_from"])
        self.assertEqual("[REDACTED]", requested["arguments"]["patient_id"])
        await actor.close()

    async def test_tool_worker_does_not_block_interrupt_processing(self) -> None:
        release = asyncio.Event()

        async def slow_tool(intent: ToolIntent) -> ToolResult:
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED)

        actor, telephony, voice, _, _ = self.build_actor(control=FakeControlPlane(slow_tool))
        await actor.start()
        chunk = AudioChunk(b"assistant", 1, 100)
        await actor.dispatch(
            SessionEvent("voice.audio", {"chunk": chunk, "item_id": "item-1", "audio_end_ms": 700})
        )
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "search_slots", OperationKind.READ)},
            )
        )
        await actor.dispatch(SessionEvent("caller.speech_started"))
        self.assertEqual(1, telephony.clear_count)
        self.assertEqual(1, voice.cancel_count)
        release.set()
        await actor.wait_idle()
        await actor.close()

    async def test_unknown_write_outcome_enters_reconciliation_required(self) -> None:
        async def fail_write(intent: ToolIntent) -> ToolResult:
            raise TimeoutError("synthetic timeout")

        actor, _, voice, _, _ = self.build_actor(control=FakeControlPlane(fail_write))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "create_appointment", OperationKind.WRITE)},
            )
        )
        await actor.wait_idle()
        self.assertEqual(SessionState.RECONCILIATION_REQUIRED, actor.state)
        self.assertEqual(ToolStatus.UNKNOWN, voice.tool_results[0].status)
        await actor.close()

    async def test_read_failure_is_typed_and_returned_without_reconciliation_state(self) -> None:
        async def fail_read(intent: ToolIntent) -> ToolResult:
            raise TimeoutError("synthetic read timeout")

        actor, _, voice, _, _ = self.build_actor(control=FakeControlPlane(fail_read))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "search_slots", OperationKind.READ)},
            )
        )
        await actor.wait_idle()
        self.assertEqual(SessionState.ACTIVE, actor.state)
        self.assertEqual(ToolStatus.FAILED, voice.tool_results[0].status)
        await actor.close()

    async def test_mismatched_write_result_fails_closed(self) -> None:
        async def wrong_result(intent: ToolIntent) -> ToolResult:
            return ToolResult("another-operation", ToolStatus.SUCCEEDED)

        actor, _, voice, _, sink = self.build_actor(control=FakeControlPlane(wrong_result))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "create_appointment", OperationKind.WRITE)},
            )
        )
        await actor.wait_idle()
        self.assertEqual(SessionState.RECONCILIATION_REQUIRED, actor.state)
        self.assertEqual("op-1", voice.tool_results[0].operation_id)
        self.assertEqual(ToolStatus.UNKNOWN, voice.tool_results[0].status)
        self.assertTrue(
            any(event["event_type"] == "tool.result_mismatch" for event in sink.events)  # type: ignore[attr-defined]
        )
        await actor.close()

    async def test_hangup_during_authorized_write_drains_worker(self) -> None:
        release = asyncio.Event()

        async def slow_write(intent: ToolIntent) -> ToolResult:
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED)

        actor, telephony, voice, _, _ = self.build_actor(control=FakeControlPlane(slow_write))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "create_appointment", OperationKind.WRITE)},
            )
        )
        await actor.dispatch(SessionEvent("telephony.stop", {"reason": "caller_hangup"}))
        self.assertEqual(SessionState.DRAINING, actor.state)
        self.assertEqual(1, len(telephony.close_reasons))
        release.set()
        await asyncio.wait_for(actor.done.wait(), timeout=1)
        self.assertEqual(SessionState.CLOSED, actor.state)
        self.assertEqual([], voice.tool_results)
        self.assertEqual(1, voice.close_count)

    async def test_mid_call_voice_failure_isolated_and_transferred(self) -> None:
        actor, telephony, voice, _, _ = self.build_actor()
        await actor.start()
        await actor.dispatch(SessionEvent("voice.error", {"reason": "provider_disconnect"}))
        await actor.done.wait()
        self.assertEqual(SessionState.FAILED, actor.state)
        self.assertEqual(["front-desk"], telephony.transfers)
        self.assertEqual(1, voice.close_count)

    async def test_voice_failure_during_write_drains_before_terminal_failure(self) -> None:
        release = asyncio.Event()

        async def slow_write(intent: ToolIntent) -> ToolResult:
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED)

        actor, telephony, voice, _, _ = self.build_actor(control=FakeControlPlane(slow_write))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "create_appointment", OperationKind.WRITE)},
            )
        )
        await actor.dispatch(SessionEvent("voice.error", {"reason": "provider_disconnect"}))
        self.assertEqual(SessionState.DRAINING, actor.state)
        self.assertEqual(1, len(telephony.close_reasons))
        release.set()
        await asyncio.wait_for(actor.done.wait(), timeout=1)
        self.assertEqual(SessionState.FAILED, actor.state)
        self.assertEqual(1, voice.close_count)

    async def test_hangup_before_unknown_write_result_ends_in_reconciliation_state(self) -> None:
        release = asyncio.Event()

        async def unknown_write(intent: ToolIntent) -> ToolResult:
            await release.wait()
            raise TimeoutError("synthetic unknown outcome")

        actor, _, _, _, _ = self.build_actor(control=FakeControlPlane(unknown_write))
        await actor.start()
        await actor.dispatch(
            SessionEvent(
                "tool.requested",
                {"intent": ToolIntent("op-1", "create_appointment", OperationKind.WRITE)},
            )
        )
        await actor.dispatch(SessionEvent("telephony.stop", {"reason": "caller_hangup"}))
        release.set()
        await asyncio.wait_for(actor.done.wait(), timeout=1)
        self.assertEqual(SessionState.RECONCILIATION_REQUIRED, actor.state)


if __name__ == "__main__":
    unittest.main()
