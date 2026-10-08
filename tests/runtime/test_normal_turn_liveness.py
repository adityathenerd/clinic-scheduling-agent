"""V09: an ordinary ACTIVE turn must not disappear after exact speech."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import unittest

from clinic_agent.call_mechanics import AsyncWorkerPool, CallSessionActor, InMemoryEventSink
from clinic_agent.call_mechanics.models import (
    AudioChunk, CallDescriptor, CallDirection, SessionEvent, ToolIntent,
    OperationKind, ToolResult, ToolStatus,
)
from clinic_agent.runtime.transcript_evaluator import evaluate_voice_events
from clinic_agent.runtime.voice_log import load_voice_events
from tests.call_mechanics.fakes import FakeControlPlane, FakeTelephony, FakeVoice
from appointment_harness.fixtures import load_fixture
from appointment_harness.service import AppointmentHarness
from clinic_agent.providers.openai_live import GPTLiveVoiceEngine
from clinic_agent.runtime.voice_control_plane import VoiceAgentControlPlane
from clinic_agent.control_plane.state_machine import ConversationStateMachine, IdentityDecision
from tests.runtime.test_voice_control_plane import FakeIdentityClassifier, context
from tests.providers.test_openai_live import FakeConnection


async def wait_for(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 1)


class NormalTurnWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def build(self, *, control=None, voice=None):
        control = control or FakeControlPlane()
        voice, telephony = voice or FakeVoice(instruction_sequence=100), FakeTelephony()
        sink, pool = InMemoryEventSink(), AsyncWorkerPool(4)
        actor = CallSessionActor(CallDescriptor('CA-liveness', CallDirection.INBOUND),
                                 telephony, voice, control, sink, pool, turn_completion_delay=0)
        actor._normal_turn_progress_timeout = 0.02
        await actor.start()
        self.addAsyncCleanup(actor.close)
        return actor, telephony, voice, control, sink, pool

    async def normal_turn(self, actor, sink):
        await actor.dispatch(SessionEvent('caller.speech_started'))
        await actor.dispatch(SessionEvent('voice.input_transcript.delta',
                                         {'delta': 'When do I have my next appointment here'}))
        await actor.dispatch(SessionEvent('caller.speech_stopped'))
        await wait_for(lambda: any(e['event_type'] == 'caller.turn' for e in sink.events))

    async def test_exact_takeover_resumes_once_for_next_ordinary_turn(self):
        control = FakeControlPlane(application_owns_completed_turn=True,
                                   completed_turn_directive='say_exactly: Identity confirmed.')
        actor, _, voice, _, sink, pool = await self.build(control=control)
        actor._normal_turn_progress_timeout = 0.2
        await self.normal_turn(actor, sink)
        await wait_for(lambda: len(voice.instructions) == 1)
        control._application_owns_completed_turn = False
        control._completed_turn_directive = None
        sink.events.clear()
        await self.normal_turn(actor, sink)
        await wait_for(lambda: len(voice.instructions) >= 2)
        self.assertIn('Resume normal conversation', voice.instructions[1])
        self.assertIn('latest completed caller turn already in the conversation', voice.instructions[1])
        self.assertNotIn('When do I have my next appointment here', voice.instructions[1])
        await actor.dispatch(SessionEvent('caller.turn_completion_processed',
                                         {'turn_epoch': 2, 'directive': None, 'error': None}))
        self.assertEqual(1, sum('Resume normal conversation' in i for i in voice.instructions))
        await actor.dispatch(SessionEvent('voice.output_transcript.delta',
                                         {'delta': 'Let me check.', 'audio_sequence': 100}))
        self.assertIsNone(actor._normal_turn_watchdog_task)
        await asyncio.sleep(0.04)
        self.assertNotIn('control.normal_turn_liveness_failed', [e['event_type'] for e in sink.events])

    async def test_silent_normal_turn_logs_error_and_one_recovery_without_tools(self):
        actor, _, voice, control, sink, _ = await self.build()
        await self.normal_turn(actor, sink)
        await wait_for(lambda: any(e['event_type'] == 'control.normal_turn_recovery_appended' for e in sink.events))
        failures = [e for e in sink.events if e['event_type'] == 'control.normal_turn_liveness_failed']
        self.assertEqual(1, len(failures))
        self.assertEqual(1, len(voice.instructions))
        self.assertIn('Please repeat', voice.instructions[0])
        self.assertEqual([], control.tool_calls)

    async def test_delegation_progress_cancels_watchdog_during_slow_backend(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(intent):
            entered.set()
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED, payload={'result': {'status': 'ok'}})

        actor, _, voice, control, sink, pool = await self.build(control=FakeControlPlane(handler=handler))
        await self.normal_turn(actor, sink)
        await actor.dispatch(SessionEvent('delegation.created', {'delegation_id': 'slow-backend'}))
        await actor.dispatch(SessionEvent('backend.response_started', {'delegation_id': 'slow-backend'}))
        await actor.dispatch(SessionEvent('backend.tool_requested', {'intent': ToolIntent(
            'read-once', 'list_appointments', OperationKind.READ, delegation_id='slow-backend')}))
        await entered.wait()
        await asyncio.sleep(0.04)
        self.assertNotIn('control.normal_turn_liveness_failed', [e['event_type'] for e in sink.events])
        self.assertEqual([], voice.instructions)
        self.assertEqual(1, len(control.tool_calls))
        release.set()
        await pool.wait_idle()

    async def test_new_speech_cancels_old_watchdog_and_stale_timeout_cannot_recover(self):
        actor, _, voice, _, sink, _ = await self.build()
        actor._normal_turn_progress_timeout = 0.2
        await self.normal_turn(actor, sink)
        await wait_for(lambda: any(e['event_type'] == 'control.normal_turn_watchdog_started' for e in sink.events))
        token = next(e['activity_token'] for e in sink.events if e['event_type'] == 'control.normal_turn_watchdog_started')
        await actor.dispatch(SessionEvent('caller.speech_started'))
        await actor.dispatch(SessionEvent('control.normal_turn_timeout', {'turn_epoch': 1, 'activity_token': token}))
        await asyncio.sleep(0.04)
        self.assertEqual([], voice.instructions)

    async def test_audio_progress_cancels_same_turn_watchdog(self):
        actor, telephony, _, _, sink, _ = await self.build()
        actor._normal_turn_progress_timeout = 0.2
        await self.normal_turn(actor, sink)
        await actor.dispatch(SessionEvent('voice.audio', {'chunk': AudioChunk(b'response', 100, 20),
                              'item_id': 'current', 'audio_end_ms': 20}))
        self.assertIsNone(actor._normal_turn_watchdog_task)
        self.assertEqual([b'response'], [c.payload for c in telephony.sent_audio])
        self.assertEqual('assistant_audio', next(e['progress_kind'] for e in sink.events
                         if e['event_type'] == 'control.normal_turn_progress'))

    async def test_previous_turn_backend_does_not_discharge_new_turn_watchdog(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def handler(intent):
            entered.set()
            await release.wait()
            return ToolResult(intent.operation_id, ToolStatus.SUCCEEDED, payload={})

        actor, _, voice, control, sink, pool = await self.build(control=FakeControlPlane(handler=handler))
        await actor.dispatch(SessionEvent('delegation.created', {'delegation_id': 'previous'}))
        await actor.dispatch(SessionEvent('backend.tool_requested', {'intent': ToolIntent(
            'previous-read', 'list_appointments', OperationKind.READ, delegation_id='previous')}))
        try:
            await entered.wait()
            actor._exact_reply_takeover_active = True
            await self.normal_turn(actor, sink)
            await actor.dispatch(SessionEvent('backend.response_started', {'delegation_id': 'previous'}))
            await wait_for(lambda: any(e['event_type'] == 'control.normal_turn_recovery_appended'
                                      for e in sink.events))
            self.assertEqual(0, control.tool_calls[0].state_revision)
        finally:
            release.set()
            await pool.wait_idle()
        self.assertEqual(1, len(voice.instructions))
        self.assertNotIn('Resume normal conversation', voice.instructions[0])
        self.assertIn('control.normal_turn_liveness_failed', [e['event_type'] for e in sink.events])

    async def test_suppressed_old_output_does_not_cancel_watchdog(self):
        actor, telephony, _, _, sink, _ = await self.build()
        actor._suppress_audio_before_sequence = 100
        actor._assistant_transcript_min_sequence = 100
        await self.normal_turn(actor, sink)
        await actor.dispatch(SessionEvent('voice.audio', {'chunk': AudioChunk(b'old', 99, 20),
                              'item_id': 'old', 'audio_end_ms': 20}))
        await actor.dispatch(SessionEvent('voice.output_transcript.delta',
                                         {'delta': 'Old exact reply', 'audio_sequence': 99}))
        await wait_for(lambda: any(e['event_type'] == 'control.normal_turn_liveness_failed' for e in sink.events))
        self.assertEqual([], telephony.sent_audio)
        self.assertIn('control.normal_turn_liveness_failed', [e['event_type'] for e in sink.events])

    async def test_trailing_exact_reply_output_after_barge_in_cannot_claim_new_turn(self):
        actor, telephony, voice, _, sink, _ = await self.build()
        actor._normal_turn_progress_timeout = 0.2
        actor._exact_reply_takeover_active = True
        actor._exact_reply_takeover_epoch = 0
        await actor.dispatch(SessionEvent('caller.speech_started'))
        await actor.dispatch(SessionEvent('voice.input_transcript.delta',
                                         {'delta': 'When was my appointment?'}))
        await actor.dispatch(SessionEvent('caller.speech_stopped'))

        # These are late frames from the interrupted identity-confirmation reply,
        # not an answer to the appointment question.
        await actor.dispatch(SessionEvent('voice.audio', {'chunk': AudioChunk(b'late identity', 101, 20),
                              'item_id': 'gpt-live-output', 'audio_end_ms': 20}))
        await actor.dispatch(SessionEvent('voice.output_transcript.delta',
                                         {'delta': 'Thank you for confirming.',
                                          'audio_sequence': 101}))

        await wait_for(lambda: any('Resume normal conversation' in item for item in voice.instructions))
        self.assertEqual([], telephony.sent_audio)
        self.assertFalse(any(e['event_type'] == 'control.normal_turn_progress'
                             and e.get('progress_kind') in {'assistant_audio', 'assistant_transcript'}
                             for e in sink.events))
        suppressed = [e for e in sink.events if e['event_type'] in {
            'assistant.audio_suppressed', 'assistant.transcript_suppressed'}]
        self.assertEqual(2, len(suppressed))
        self.assertTrue(all(e.get('reason') == 'stale_exact_reply_after_barge_in'
                            for e in suppressed))

    async def test_progress_before_completion_prevents_resume_and_duplicate_backend_work(self):
        actor, _, voice, _, sink, pool = await self.build()
        actor._exact_reply_takeover_active = True
        actor._turn_completion_delay = 0.1
        await actor.dispatch(SessionEvent('caller.speech_started'))
        await actor.dispatch(SessionEvent('voice.input_transcript.delta', {'delta': 'When is my appointment?'}))
        await actor.dispatch(SessionEvent('caller.speech_stopped'))
        await actor.dispatch(SessionEvent('delegation.created', {'delegation_id': 'already-started'}))
        await actor.dispatch(SessionEvent('caller.turn_completed', {'turn_epoch': 1}))
        await pool.wait_idle()
        await wait_for(lambda: any(e['event_type'] == 'caller.turn' for e in sink.events))
        self.assertEqual([], voice.instructions)
        self.assertIsNone(actor._normal_turn_watchdog_task)

    async def test_hangup_cancels_watchdog_without_post_hangup_recovery(self):
        actor, _, voice, _, sink, _ = await self.build()
        actor._normal_turn_progress_timeout = 0.2
        await self.normal_turn(actor, sink)
        await actor.close('remote_stop')
        await asyncio.sleep(0.03)
        self.assertEqual([], voice.instructions)
        self.assertIsNone(actor._normal_turn_watchdog_task)

    async def test_resume_delivery_error_is_logged_and_closes_without_tool_retry(self):
        class Voice(FakeVoice):
            async def append_instructions(self, content):
                if 'Resume normal conversation' in content:
                    raise TimeoutError('synthetic missing ack')
                return await super().append_instructions(content)

        actor, _, _, control, sink, _ = await self.build(voice=Voice())
        actor._exact_reply_takeover_active = True
        await self.normal_turn(actor, sink)
        await asyncio.wait_for(actor.done.wait(), 1)
        self.assertEqual([], control.tool_calls)
        failures = [e for e in sink.events if e['event_type'] == 'control.normal_turn_liveness_failed']
        self.assertEqual('resume_delivery_failed', failures[0]['reason'])

    async def test_recovery_delivery_error_is_explicit_and_closes_the_transport(self):
        class Voice(FakeVoice):
            async def append_instructions(self, content):
                raise TimeoutError('synthetic recovery ack timeout')

        actor, _, _, control, sink, _ = await self.build(voice=Voice())
        await self.normal_turn(actor, sink)
        await asyncio.wait_for(actor.done.wait(), 1)
        self.assertEqual([], control.tool_calls)
        self.assertIn('control.normal_turn_recovery_failed', [e['event_type'] for e in sink.events])

    async def test_late_delegation_after_recovery_cannot_start_tools(self):
        actor, _, voice, control, sink, _ = await self.build()
        await self.normal_turn(actor, sink)
        await wait_for(lambda: any(e['event_type'] == 'control.normal_turn_recovery_appended' for e in sink.events))
        await actor.dispatch(SessionEvent('delegation.created', {'delegation_id': 'late'}))
        await actor.dispatch(SessionEvent('backend.tool_requested', {'intent': ToolIntent(
            'late-read', 'list_appointments', OperationKind.READ, delegation_id='late')}))
        self.assertEqual([], control.tool_calls)
        self.assertEqual(1, len(voice.instructions))

    async def test_live_wire_replay_resumes_stalled_next_appointment_turn_and_reads_once(self):
        class Connection(FakeConnection):
            async def send(self, event):
                await super().send(event)
                if event['type'] == 'session.instructions.append' and 'Resume normal conversation' in event['content']:
                    self.events.put_nowait({'type': 'session.delegation.created', 'delegation': {'id': 'resume-read'}})
                    self.events.put_nowait({'type': 'response.event', 'delegation_id': 'resume-read',
                        'event': {'type': 'response.created', 'response': {'id': 'read-response'}}})
                    self.events.put_nowait({'type': 'response.event', 'delegation_id': 'resume-read',
                        'event': {'type': 'response.output_item.done', 'item': {'type': 'function_call',
                                  'call_id': 'list-once', 'name': 'list_appointments', 'arguments': '{}'}}})
                if event['type'] == 'response.item.create':
                    result = json.loads(event['item']['output'])['payload']['result']
                    summary = result['appointments'][0]['patient_facing_summary']
                    self.events.put_nowait({'type': 'session.output_transcript.delta', 'delta': summary})
                    self.events.put_nowait({'type': 'session.output_audio.delta',
                                           'delta': base64.b64encode(b'authoritative appointment readback').decode()})

        store, clock = load_fixture()
        harness = AppointmentHarness(store, clock)
        sink, pool = InMemoryEventSink(), AsyncWorkerPool(4)
        state = ConversationStateMachine(patient_display_name='Asha Rao')
        control = VoiceAgentControlPlane(harness, session_id='voice-CA-replay-v09', patient_id='patient-001',
                    prompt_context=context(), state_machine=state, event_sink=sink,
                    identity_classifier=FakeIdentityClassifier(IdentityDecision.AFFIRMED))
        connection = Connection([{'type': 'session.started', 'session': {}}])
        actor = None

        async def receive(event):
            if actor is not None and not actor.done.is_set():
                await actor.dispatch(event)

        voice = GPTLiveVoiceEngine(receive, connection_factory=lambda: connection, close_timeout=0)
        telephony = FakeTelephony()
        actor = CallSessionActor(CallDescriptor('CA-replay-v09', CallDirection.OUTBOUND, destination='synthetic'),
                                 telephony, voice, control, sink, pool, turn_completion_delay=0,
                                 normal_turn_progress_timeout=0.2)
        await actor.start()
        self.addAsyncCleanup(actor.close)
        await actor.dispatch(SessionEvent('caller.speech_started'))
        connection.events.put_nowait({'type': 'session.input_transcript.delta', 'delta': 'Yes, this is Asha Rao speaking'})
        await wait_for(lambda: bool(control._transcript_fragments))
        await actor.dispatch(SessionEvent('caller.speech_stopped'))
        await wait_for(lambda: any(e['event_type'] == 'control.completed_turn_directive' for e in sink.events))
        exact = control.last_observation.directive.split(':', 1)[1].strip()
        connection.events.put_nowait({'type': 'session.output_transcript.delta', 'delta': exact})
        await wait_for(lambda: bool(actor._assistant_transcript_fragments))
        await actor.dispatch(SessionEvent('caller.speech_started'))
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/voice_v09_active_turn_stall.jsonl'
        recorded = next(e['transcript'] for e in load_voice_events(fixture) if e['event_type'] == 'patient.turn_observed')
        connection.events.put_nowait({'type': 'session.input_transcript.delta', 'delta': recorded})
        await wait_for(lambda: bool(control._transcript_fragments))
        await actor.dispatch(SessionEvent('caller.speech_stopped'))
        await wait_for(lambda: bool(telephony.sent_audio))
        await pool.wait_idle()
        self.assertEqual(1, sum('Resume normal conversation' in e.get('content', '') for e in connection.sent))
        reads = [e for e in sink.events if e['event_type'] == 'tool.requested' and e['tool_name'] == 'list_appointments']
        self.assertEqual(1, len(reads))
        self.assertEqual([b'authoritative appointment readback'], [c.payload for c in telephony.sent_audio])
        self.assertNotIn('control.normal_turn_liveness_failed', [e['event_type'] for e in sink.events])
        self.assertEqual(0, harness.store.reschedule_sequence)


class NormalTurnEvaluatorControls(unittest.TestCase):
    def test_recorded_successful_reschedule_keeps_both_exact_timezone_failures(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/voice_v09_reschedule_timezone_omission.jsonl'
        result = evaluate_voice_events(load_voice_events(fixture))
        self.assertNotIn('normal_active_turn_no_progress', {i.code for i in result.issues})
        exact = next(i for i in result.hard_failures if i.code == 'exact_application_reply_not_honored')
        self.assertEqual(2, len(exact.evidence))
        self.assertEqual(1, result.metrics['verified_writes'])

    def test_recorded_clinic_confirmation_stays_clean(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/voice_v09_clinic_confirmation_pass.jsonl'
        result = evaluate_voice_events(load_voice_events(fixture))
        self.assertEqual('pass', result.status)
        self.assertEqual((), result.issues)

    def test_recorded_v09_stall_has_specific_negative_control_failure(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/voice_v09_active_turn_stall.jsonl'
        result = evaluate_voice_events(load_voice_events(fixture))
        issue = next((i for i in result.hard_failures if i.code == 'normal_active_turn_no_progress'), None)
        self.assertIsNotNone(issue)
        self.assertTrue(any('turn_epoch=2' in item for item in issue.evidence))

    @staticmethod
    def rows(*progress):
        origin = datetime(2026, 10, 7, 14, 26, tzinfo=timezone.utc)
        def event(second, kind, **metadata):
            return {'event_type': kind, 'occurred_at': (origin + timedelta(seconds=second)).isoformat(), **metadata}
        return [event(0, 'call.started'), event(1, 'patient.turn_observed',
                workflow_before='active', workflow_after='active', transcript='When is my appointment?'),
                event(1, 'caller.turn', turn_epoch=2), *[event(*p) for p in progress],
                event(45, 'call.remote_stop'), event(46, 'call.ended')]

    def test_backend_progress_allows_legitimate_latency_normal_control(self):
        rows = self.rows((2, 'delegation.created'), (3, 'backend.response_started'),
                         (20, 'tool.requested'), (35, 'assistant.turn'))
        result = evaluate_voice_events(rows)
        self.assertNotIn('normal_active_turn_no_progress', {i.code for i in result.issues})

    def test_other_call_progress_cannot_mask_stuck_turn(self):
        rows = self.rows()
        for row in rows:
            row['provider_call_id'] = 'CA-stalled'
        rows.insert(3, {'event_type': 'assistant.turn', 'provider_call_id': 'CA-other',
                        'occurred_at': '2026-10-07T14:26:03+00:00', 'transcript': 'Hello'})
        self.assertIn('normal_active_turn_no_progress', {i.code for i in evaluate_voice_events(rows).issues})

    def test_watchdog_failure_is_detected_before_any_hangup(self):
        rows = self.rows()[:3]
        rows.append({'event_type': 'control.normal_turn_liveness_failed', 'turn_epoch': 2,
                     'occurred_at': '2026-10-07T14:26:06+00:00'})
        self.assertIn('normal_active_turn_no_progress', {i.code for i in evaluate_voice_events(rows).hard_failures})

    def test_wrong_epoch_progress_cannot_mask_stuck_turn(self):
        rows = self.rows()
        rows.insert(3, {'event_type': 'control.normal_turn_progress', 'turn_epoch': 1,
                        'occurred_at': '2026-10-07T14:26:02+00:00'})
        self.assertIn('normal_active_turn_no_progress', {i.code for i in evaluate_voice_events(rows).hard_failures})

    def test_unattributed_audio_after_boundary_cannot_mask_stuck_turn(self):
        rows = self.rows()
        rows.insert(1, {'event_type': 'caller.turn_boundary_detected', 'turn_epoch': 2,
                        'occurred_at': '2026-10-07T14:26:00.500000+00:00'})
        rows.insert(2, {'event_type': 'control.normal_turn_progress', 'turn_epoch': 2,
                        'activity_token': 'normal-turn-2-2', 'progress_kind': 'assistant_audio',
                        'occurred_at': '2026-10-07T14:26:00.750000+00:00'})
        rows.insert(5, {'event_type': 'control.normal_turn_completed', 'turn_epoch': 2,
                        'activity_token': 'normal-turn-2-2', 'progress_seen': True,
                        'occurred_at': '2026-10-07T14:26:01.100000+00:00'})
        result = evaluate_voice_events(rows)
        self.assertIn('normal_active_turn_no_progress', {i.code for i in result.hard_failures})

    def test_progress_after_deadline_is_still_a_liveness_failure(self):
        result = evaluate_voice_events(self.rows((20, 'delegation.created')))
        self.assertIn('normal_active_turn_no_progress', {i.code for i in result.hard_failures})

    def test_transition_without_state_does_not_mask_stuck_turn(self):
        result = evaluate_voice_events(self.rows((2, 'workflow.transition')))
        self.assertIn('normal_active_turn_no_progress', {i.code for i in result.hard_failures})

    def test_explicit_failure_is_detected_without_timestamps(self):
        rows = self.rows()[:3]
        rows.append({'event_type': 'control.normal_turn_liveness_failed', 'turn_epoch': 2})
        for row in rows:
            row.pop('occurred_at', None)
        self.assertIn('normal_active_turn_no_progress', {i.code for i in evaluate_voice_events(rows).hard_failures})
