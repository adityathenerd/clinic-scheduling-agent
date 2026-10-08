"""Recorded V07 race: speech-end precedes the last transcript delta."""

from __future__ import annotations

import asyncio
import base64
import unittest

from clinic_agent.call_mechanics import AsyncWorkerPool, CallSessionActor, InMemoryEventSink
from clinic_agent.call_mechanics.models import AudioChunk, CallDescriptor, CallDirection, OperationKind, SessionEvent, ToolIntent, ToolResult, ToolStatus
from clinic_agent.control_plane.state_machine import ConfirmationDecision, IdentityDecision, WorkflowState
from clinic_agent.providers.openai_live import GPTLiveVoiceEngine
from tests.providers import test_openai_live as live_tests
from tests.call_mechanics.fakes import FakeControlPlane, FakeTelephony, FakeVoice
from tests.runtime import test_voice_control_plane as plane_tests


class TranscriptSink:
    def __init__(self):
        self.events = []

    async def record(self, event_type, **metadata):
        self.events.append({"event_type": event_type, **metadata})


class ConfirmationSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await plane_tests.VoiceControlPlaneTests.asyncSetUp(self)

    async def prepare(self) -> dict[str, str]:
        classifier = plane_tests.FakeIdentityClassifier(IdentityDecision.AFFIRMED)
        self.subject.identity_classifier = classifier
        await self.subject.bootstrap(CallDescriptor("CA-race", CallDirection.OUTBOUND, destination="synthetic"))
        await self.subject.observe_patient_transcript_delta("Yes, this is Asha Rao")
        await self.subject.complete_patient_turn(turn_epoch=1)
        await self.subject.handle_tool_intent(ToolIntent("slots", "search_slots", OperationKind.READ, {
            "appointment_type": "dermatology-followup", "date_from": "2026-10-23", "date_to": "2026-10-23",
            "provider_id": "provider-mehta", "location_id": "downtown",
        }))
        arguments = {"appointment_id": "appointment-existing", "replacement_slot_id": "slot-mehta-20261023-1400"}
        await self.subject.handle_tool_intent(ToolIntent("proposal", "edit_appointment", OperationKind.WRITE, arguments, state_revision=6))
        await self.subject.arm_pending_confirmation(after_turn_epoch=6)
        return arguments

    async def test_late_yes_fragment_reclassifies_complete_snapshot_and_commits_once(self) -> None:
        arguments = await self.prepare()
        snapshots = []
        subject = self.subject

        class Classifier:
            async def classify_confirmation(self, *, transcript, exact_proposal):
                snapshots.append(transcript)
                if len(snapshots) == 1:
                    await subject.observe_patient_transcript_delta(" yes")
                    return ConfirmationDecision.UNCLEAR
                return ConfirmationDecision.CONFIRMED

        self.subject.confirmation_classifier = Classifier()
        await self.subject.observe_patient_transcript_delta("Uh")
        directive = await self.subject.complete_patient_turn(turn_epoch=7)
        self.assertEqual(["Uh", "Uh yes"], snapshots)
        self.assertIn("is held for clinic review", directive or "")
        self.assertIn("remains in place until approval", directive or "")
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.state.state)
        self.assertEqual(1, self.harness.store.reschedule_sequence)
        replay = await self.subject.handle_tool_intent(ToolIntent("duplicate", "edit_appointment", OperationKind.WRITE, arguments))
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(1, self.harness.store.reschedule_sequence)

    async def test_late_correction_cannot_be_overridden_by_initial_yes(self) -> None:
        await self.prepare()
        snapshots = []
        subject = self.subject

        class Classifier:
            async def classify_confirmation(self, *, transcript, exact_proposal):
                snapshots.append(transcript)
                if len(snapshots) == 1:
                    await subject.observe_patient_transcript_delta(", but make it Monday instead")
                    return ConfirmationDecision.CONFIRMED
                return ConfirmationDecision.CORRECTION

        self.subject.confirmation_classifier = Classifier()
        await self.subject.observe_patient_transcript_delta("Yes")
        directive = await self.subject.complete_patient_turn(turn_epoch=7)
        self.assertEqual(2, len(snapshots))
        self.assertIn("correction", (directive or "").lower())
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertIsNone(self.subject.runtime.pending)
        self.assertEqual(0, self.harness.store.reschedule_sequence)

    async def test_second_revision_recovers_explicitly_without_a_third_classification(self) -> None:
        await self.prepare()
        snapshots = []
        subject = self.subject

        class Classifier:
            async def classify_confirmation(self, *, transcript, exact_proposal):
                snapshots.append(transcript)
                await subject.observe_patient_transcript_delta(" yes")
                return ConfirmationDecision.CONFIRMED

        self.subject.confirmation_classifier = Classifier()
        await self.subject.observe_patient_transcript_delta("Uh")
        directive = await self.subject.complete_patient_turn(turn_epoch=7)
        self.assertEqual(2, len(snapshots))
        self.assertTrue((directive or "").startswith("say_exactly:"))
        self.assertIn("have not made", directive or "")
        self.assertEqual(WorkflowState.CONFIRMATION_PENDING, self.state.state)
        self.assertEqual(0, self.harness.store.reschedule_sequence)

    async def test_classifier_timeout_recovers_without_mutation(self):
        await self.prepare()
        started, cancelled = asyncio.Event(), asyncio.Event()

        class Classifier:
            async def classify_confirmation(self, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        self.subject.confirmation_classifier = Classifier()
        self.subject._confirmation_classification_timeout = 0.01
        await self.subject.observe_patient_transcript_delta("Uh yes")
        directive = await self.subject.complete_patient_turn(turn_epoch=7)
        self.assertTrue(started.is_set() and cancelled.is_set())
        self.assertIn("have not made", directive or "")
        self.assertEqual(0, self.harness.store.reschedule_sequence)
        self.assertEqual(WorkflowState.CONFIRMATION_PENDING, self.state.state)

    async def test_resumed_speech_revokes_snapshot_until_new_turn_finishes(self):
        await self.prepare()
        self.subject.begin_caller_turn(7)
        entered, release = asyncio.Event(), asyncio.Event()

        class Classifier:
            async def classify_confirmation(self, **kwargs):
                entered.set()
                await release.wait()
                return ConfirmationDecision.CONFIRMED

        self.subject.confirmation_classifier = Classifier()
        await self.subject.observe_patient_transcript_delta("Yes")
        old = asyncio.create_task(self.subject.complete_patient_turn(turn_epoch=7))
        await entered.wait()
        self.subject.begin_caller_turn(8)
        release.set()
        self.assertIsNone(await old)
        self.assertEqual(0, self.harness.store.reschedule_sequence)
        await self.subject.observe_patient_transcript_delta(", no, keep my current appointment")
        self.subject.confirmation_classifier = plane_tests.FakeIdentityClassifier(IdentityDecision.AFFIRMED, ConfirmationDecision.DENIED)
        directive = await self.subject.complete_patient_turn(turn_epoch=8)
        self.assertIn("not made", directive or "")
        self.assertEqual(WorkflowState.ACTIVE, self.state.state)
        self.assertEqual(0, self.harness.store.reschedule_sequence)

    async def test_missing_transcript_prompts_instead_of_stranding_confirmation(self):
        await self.prepare()
        directive = await self.subject.complete_patient_turn(turn_epoch=7)
        self.assertIn("didn't catch", directive or "")
        self.assertEqual(0, self.harness.store.reschedule_sequence)

    async def test_speech_before_late_proposal_delivery_cannot_authorize_it(self):
        await self.prepare()
        await self.subject.arm_pending_confirmation(after_turn_epoch=7)
        self.subject.confirmation_classifier = plane_tests.FakeIdentityClassifier(IdentityDecision.AFFIRMED)
        await self.subject.observe_patient_transcript_delta("Yes")
        directive = await self.subject.complete_patient_turn(turn_epoch=7)
        self.assertIn("consolidated request", directive or "")
        self.assertEqual([], self.subject.confirmation_classifier.confirmation_calls)
        self.assertEqual(0, self.harness.store.reschedule_sequence)

    async def test_live_wire_replay_of_uh_then_yes_finishes_without_another_speech_boundary(self):
        await self.prepare()
        entered, release = asyncio.Event(), asyncio.Event()
        snapshots = []

        class Classifier:
            async def classify_confirmation(self, *, transcript, exact_proposal):
                snapshots.append(transcript)
                if len(snapshots) == 1:
                    entered.set()
                    await release.wait()
                    return ConfirmationDecision.UNCLEAR
                return ConfirmationDecision.CONFIRMED

        self.subject.confirmation_classifier = Classifier()
        sink = TranscriptSink()
        self.subject.event_sink = sink
        connection = live_tests.FakeConnection([{"type": "session.started", "session": {}}])
        telephony, pool = FakeTelephony(), AsyncWorkerPool(4)
        actor = None

        async def receive(event):
            if actor is not None and not actor.done.is_set():
                await actor.dispatch(event)

        voice = GPTLiveVoiceEngine(receive, connection_factory=lambda: connection, close_timeout=0)
        actor = CallSessionActor(CallDescriptor("CA-recorded-race", CallDirection.INBOUND), telephony, voice, self.subject, sink, pool, turn_completion_delay=0.01)
        # Replay starts after the recorded proposal, whose origin was epoch 6.
        actor._caller_turn_epoch = 6
        await actor.start()
        self.addAsyncCleanup(actor.close)
        await actor.dispatch(SessionEvent("caller.speech_started"))
        connection.events.put_nowait({"type": "session.input_transcript.delta", "delta": "Uh"})
        await live_tests.wait_until(lambda: bool(self.subject._transcript_fragments))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        connection.events.put_nowait({"type": "session.output_transcript.delta", "delta": "Thank you. I'm checking your confirmation now."})
        connection.events.put_nowait({"type": "session.output_audio.delta", "delta": base64.b64encode(b"progress").decode()})
        await asyncio.wait_for(entered.wait(), 1)
        connection.events.put_nowait({"type": "session.input_transcript.delta", "delta": " yes"})
        await live_tests.wait_until(lambda: "".join(self.subject._transcript_fragments) == "Uh yes")
        release.set()
        await pool.wait_idle()
        await live_tests.wait_until(lambda: any(e["event_type"] == "control.completed_turn_directive" for e in sink.events))
        exact = self.subject.last_observation.directive.split(":", 1)[1].strip()
        connection.events.put_nowait({"type": "session.output_transcript.delta", "delta": exact})
        connection.events.put_nowait({"type": "session.output_audio.delta", "delta": base64.b64encode(b"verified held replacement").decode()})
        await live_tests.wait_until(lambda: len(telephony.sent_audio) == 2)
        await actor.dispatch(SessionEvent("caller.speech_started"))
        self.assertEqual(["Uh", "Uh yes"], snapshots)
        self.assertEqual(WorkflowState.AWAITING_FOLLOW_UP_DECISION, self.state.state)
        self.assertEqual(1, self.harness.store.reschedule_sequence)
        self.assertEqual(["Thank you. I'm checking your confirmation now.", exact],
                         [e["transcript"] for e in sink.events if e["event_type"] == "assistant.turn"])
        self.assertEqual([b"progress", b"verified held replacement"], [a.payload for a in telephony.sent_audio])


class ExactReplyOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_reply_that_starts_before_instruction_ack_keeps_its_first_words(self):
        exact = "Please confirm this rescheduling request. Please say yes or no."

        class Connection(live_tests.FakeConnection):
            async def send(self, event):
                await super().send(event)
                if event.get("type") == "session.instructions.append":
                    self.events.put_nowait({
                        "type": "session.output_transcript.delta",
                        "delta": exact,
                    })
                    self.events.put_nowait({
                        "type": "session.output_audio.delta",
                        "delta": base64.b64encode(b"complete exact proposal").decode(),
                    })
                    self.events.put_nowait({
                        "type": "session.instructions.appended",
                        "client_event_id": event["event_id"],
                    })

        control = FakeControlPlane(
            application_owns_completed_turn=True,
            completed_turn_directive=f"say_exactly: {exact}",
        )
        connection = Connection(
            [{"type": "session.started", "session": {}}],
            auto_ack_instructions=False,
        )
        telephony, sink, pool = FakeTelephony(), TranscriptSink(), AsyncWorkerPool(4)
        actor = None

        async def receive(event):
            if actor is not None and not actor.done.is_set():
                await actor.dispatch(event)

        voice = GPTLiveVoiceEngine(
            receive,
            connection_factory=lambda: connection,
            close_timeout=0,
        )
        actor = CallSessionActor(
            CallDescriptor("CA-pre-ack-exact", CallDirection.INBOUND),
            telephony,
            voice,
            control,
            sink,
            pool,
            turn_completion_delay=0,
        )
        await actor.start()
        self.addAsyncCleanup(actor.close)
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("voice.input_transcript.delta", {"delta": "yes"}))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await live_tests.wait_until(lambda: bool(telephony.sent_audio))
        await actor.dispatch(SessionEvent("caller.speech_started"))

        self.assertEqual([b"complete exact proposal"], [c.payload for c in telephony.sent_audio])
        self.assertEqual(
            [exact],
            [e["transcript"] for e in sink.events if e["event_type"] == "assistant.turn"],
        )

    async def test_progress_has_ack_cutoff_and_duplicate_stop_does_not_repeat_it(self):
        control = FakeControlPlane(application_owns_completed_turn=True, application_gate_progress_message="Thank you. I'm checking your confirmation now.")
        voice, telephony = FakeVoice(instruction_sequence=5), FakeTelephony()
        actor = CallSessionActor(CallDescriptor("CA-progress", CallDirection.INBOUND), telephony, voice, control, TranscriptSink(), AsyncWorkerPool(4), turn_completion_delay=0.1)
        await actor.start()
        self.addAsyncCleanup(actor.close)
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await actor.dispatch(SessionEvent("voice.audio", {"chunk": AudioChunk(b"autonomous old audio", 4, 20), "item_id": "old", "audio_end_ms": 20}))
        self.assertEqual([], telephony.sent_audio)
        self.assertEqual(1, len(voice.instructions))
        self.assertEqual([], voice.commentary)

    async def test_progress_is_separate_from_exact_reply_and_late_old_audio_is_dropped(self):
        control = FakeControlPlane(completed_turn_directive="say_exactly: Identity confirmed.", application_owns_completed_turn=True,
                                   application_gate_progress_message="Thank you. I'm checking your identity confirmation now.")
        voice = FakeVoice(instruction_sequence=5)
        sink = TranscriptSink()
        pool = AsyncWorkerPool(4)
        telephony = FakeTelephony()
        actor = CallSessionActor(CallDescriptor("CA-owned", CallDirection.INBOUND), telephony, voice, control, sink, pool, turn_completion_delay=0.01)
        await actor.start()
        self.addAsyncCleanup(actor.close)
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("voice.input_transcript.delta", {"delta": "Yes"}))
        await actor.dispatch(SessionEvent("caller.speech_stopped"))
        await actor.dispatch(SessionEvent("voice.output_transcript.delta", {"delta": "Thank you. I'm checking your identity confirmation now.", "audio_sequence": 5}))
        await asyncio.sleep(0.03)
        await pool.wait_idle()
        await asyncio.sleep(0.01)
        authorized = AudioChunk(b"identity confirmed", 5, 40)
        await actor.dispatch(SessionEvent("voice.audio", {"chunk": authorized, "item_id": "owned", "audio_end_ms": 40}))
        await actor.dispatch(SessionEvent("voice.audio", {"chunk": AudioChunk(b"late autonomous", 4, 20), "item_id": "old", "audio_end_ms": 20}))
        await actor.dispatch(SessionEvent("voice.output_transcript.delta", {"delta": "Identity confirmed.", "audio_sequence": 5}))
        await actor.dispatch(SessionEvent("caller.speech_started"))
        turns = [e["transcript"] for e in sink.events if e["event_type"] == "assistant.turn"]
        self.assertEqual(["Thank you. I'm checking your identity confirmation now.", "Identity confirmed."], turns)
        self.assertEqual([authorized], telephony.sent_audio)

    async def test_proposal_blocks_autonomous_output_before_tool_result_is_submitted(self):
        ready, release = asyncio.Event(), asyncio.Event()

        async def handler(intent):
            ready.set()
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED, payload={"result": {"status": "confirmation_required"},
                "application_directive": "say_exactly: Exact proposed rescheduling. Please say yes or no."})

        control = FakeControlPlane(handler=handler)
        voice = FakeVoice(instruction_sequence=5)
        telephony = FakeTelephony()
        sink, pool = TranscriptSink(), AsyncWorkerPool(4)
        actor = CallSessionActor(CallDescriptor("CA-proposal-race", CallDirection.INBOUND), telephony, voice, control, sink, pool)
        await actor.start()
        self.addAsyncCleanup(actor.close)
        await actor.dispatch(SessionEvent("caller.speech_started"))
        await actor.dispatch(SessionEvent("tool.requested", {"intent": ToolIntent("proposal", "edit_appointment", OperationKind.WRITE)}))
        await ready.wait()
        await actor.dispatch(SessionEvent("voice.output_transcript.delta", {"delta": "Okay, let's set that up.", "audio_sequence": 4}))
        await actor.dispatch(SessionEvent("voice.audio", {"chunk": AudioChunk(b"autonomous preamble", 4, 20), "item_id": "old", "audio_end_ms": 20}))
        release.set()
        await pool.wait_idle()
        await asyncio.sleep(0.01)
        await actor.dispatch(SessionEvent("voice.output_transcript.delta", {"delta": "Exact proposed rescheduling. Please say yes or no.", "audio_sequence": 5}))
        await actor.dispatch(SessionEvent("caller.speech_started"))
        self.assertEqual([], telephony.sent_audio)
        turns = [e["transcript"] for e in sink.events if e["event_type"] == "assistant.turn"]
        self.assertEqual(["Exact proposed rescheduling. Please say yes or no."], turns)
