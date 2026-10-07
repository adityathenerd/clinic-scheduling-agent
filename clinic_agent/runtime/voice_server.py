"""Production-shaped composition root for the Twilio + GPT-Live demo path."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from appointment_harness.sqlite_repository import open_sqlite_harness
from clinic_agent.agent.prompts import PromptContext
from clinic_agent.clinic_profile import (
    CLINIC_ADDRESS,
    CLINIC_NAME,
    CLINIC_TIMEZONE_LABEL,
)
from clinic_agent.call_mechanics import (
    AsyncWorkerPool,
    CallSessionActor,
    CompositeEventSink,
    InMemoryEventSink,
    JsonLinesEventSink,
)
from clinic_agent.call_mechanics.actor import SessionClosedError
from clinic_agent.control_plane.state_machine import ConversationStateMachine
from clinic_agent.providers.openai_live import GPTLiveVoiceEngine
from clinic_agent.providers.twilio import TwilioMediaSession
from clinic_agent.runtime.config import VoiceRuntimeSettings
from clinic_agent.runtime.voice_app import create_voice_app
from clinic_agent.runtime.voice_control_plane import VoiceAgentControlPlane
from clinic_agent.runtime.identity_classifier import ResponsesIdentityTurnClassifier
from clinic_agent.runtime.outbound_call import validate_e164


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _prompt_context() -> PromptContext:
    constitution = PROJECT_ROOT / "docs" / "AGENT_CONSTITUTION.md"
    return PromptContext(
        assistant_name="Mira",
        clinic_name=CLINIC_NAME,
        clinic_address=CLINIC_ADDRESS,
        clinic_timezone_label=CLINIC_TIMEZONE_LABEL,
        approved_urgent_message=(
            "I can't assess urgent symptoms. Please contact local emergency services "
            "now, or have someone nearby help you do so."
        ),
        constitution_version="v0.2-provisional",
        constitution_hash=sha256(constitution.read_bytes()).hexdigest(),
        clinic_policy_version="2care-clinic-v0.2",
    )


def create_configured_voice_app(
    settings: VoiceRuntimeSettings,
    *,
    database_path: str | Path = PROJECT_ROOT / "data" / "clinic.db",
    event_log_path: str | Path = PROJECT_ROOT / "data" / "voice-events.jsonl",
    max_tool_concurrency: int = 16,
) -> Any:
    """Compose provider adapters around the existing guarded scheduling core."""

    harness, repository = open_sqlite_harness(database_path)
    patient_id = "patient-001"  # Deliberately bounded synthetic submission fixture.
    patient = harness.store.patients[patient_id]
    demo_destination = os.environ.get("DEMO_PATIENT_PHONE_E164", "").strip()
    if demo_destination:
        patient = replace(
            patient,
            phone_e164=validate_e164(
                demo_destination,
                field_name="DEMO_PATIENT_PHONE_E164",
            ),
        )
        harness.store.patients[patient_id] = patient
        repository.sync_store(harness.store, harness.clock)
    prompt_context = _prompt_context()
    worker_pool = AsyncWorkerPool(max_tool_concurrency)
    memory_sink = InMemoryEventSink()
    file_sink = JsonLinesEventSink(
        event_log_path,
        include_transcripts=os.environ.get("VOICE_LOG_TRANSCRIPTS", "").casefold()
        in {"1", "true", "yes"},
        echo=True,
    )
    event_sink = CompositeEventSink(memory_sink, file_sink)
    openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    identity_classifier = ResponsesIdentityTurnClassifier(openai_client)

    async def make_media_session(websocket: Any) -> TwilioMediaSession:
        return await TwilioMediaSession.create(websocket)

    def make_actor(descriptor: Any, telephony: Any) -> CallSessionActor:
        state_machine = ConversationStateMachine(
            patient_display_name=patient.display_name,
            approved_urgent_message=prompt_context.approved_urgent_message,
        )
        control_plane = VoiceAgentControlPlane(
            harness,
            session_id=f"voice-{descriptor.provider_call_id}",
            patient_id=patient_id,
            prompt_context=prompt_context,
            state_machine=state_machine,
            event_sink=event_sink,
            identity_classifier=identity_classifier,
            confirmation_classifier=identity_classifier,
            follow_up_classifier=identity_classifier,
        )
        actor: CallSessionActor | None = None

        async def deliver_voice_event(event: Any) -> None:
            if actor is None or actor.done.is_set():
                return
            try:
                await actor.dispatch(event)
            except SessionClosedError:
                return

        voice = GPTLiveVoiceEngine(
            deliver_voice_event,
            client=openai_client,
        )
        actor = CallSessionActor(
            descriptor,
            telephony,
            voice,
            control_plane,
            event_sink,
            worker_pool,
            mailbox_capacity=128,
            voice_connect_retries=1,
        )
        return actor

    app = create_voice_app(
        settings,
        media_session_factory=make_media_session,
        actor_factory=make_actor,
    )
    app.state.clinic_harness = harness
    app.state.clinic_repository = repository
    app.state.call_event_sink = event_sink
    app.state.call_events = memory_sink.events
    app.state.call_event_log_path = str(Path(event_log_path).resolve())
    app.state.tool_worker_pool = worker_pool
    return app


__all__ = ["create_configured_voice_app"]
