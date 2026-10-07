"""Interactive text entry point for the guarded scheduling agent."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from typing import Callable, Sequence
from uuid import uuid4

from appointment_harness.conversation_store import ConversationDocumentStore
from appointment_harness.models import GeneralHandoffCommand
from appointment_harness.sqlite_repository import open_sqlite_harness
from clinic_agent.agent.prompts import PromptContext, build_live_prompt
from clinic_agent.clinic_profile import (
    CLINIC_ADDRESS,
    CLINIC_NAME,
    CLINIC_TIMEZONE_LABEL,
)
from clinic_agent.control_plane.state_machine import ConversationStateMachine
from clinic_agent.runtime.text_session import (
    GuardedToolRuntime,
    ResponsesTextSession,
    text_runtime_instructions,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _record_conversation_event(
    operation: Callable[[], object], *, label: str
) -> None:
    """Keep observational storage failures outside the scheduling control path."""

    try:
        operation()
    except Exception as error:
        print(
            f"[conversation-store warning] Could not persist {label}: "
            f"{type(error).__name__}"
        )


def _context() -> PromptContext:
    constitution = PROJECT_ROOT / "docs" / "AGENT_CONSTITUTION.md"
    constitution_hash = sha256(constitution.read_bytes()).hexdigest()
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
        constitution_hash=constitution_hash,
        clinic_policy_version="2care-clinic-v0.2",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the guarded clinic scheduling agent in a text conversation."
    )
    parser.add_argument("--model", default="gpt-6-sol")
    parser.add_argument(
        "--mock-call",
        action="store_true",
        help="Run a deterministic credential-free booking call simulation.",
    )
    parser.add_argument(
        "--check-voice-config",
        action="store_true",
        help="Report which voice environment variables are present without printing values.",
    )
    parser.add_argument(
        "--save-voice-config",
        action="store_true",
        help=(
            "Persist validated voice variables from this process to the ignored "
            "project .env without printing their values."
        ),
    )
    parser.add_argument(
        "--serve-voice",
        action="store_true",
        help="Run the signed Twilio webhook and media WebSocket server.",
    )
    parser.add_argument(
        "--check-twilio-account",
        action="store_true",
        help="Read account, balance, and configured-number voice capability without writes.",
    )
    parser.add_argument(
        "--place-call",
        metavar="E164_NUMBER",
        help="Place one outbound Twilio call to an E.164 destination.",
    )
    parser.add_argument(
        "--yes-place-real-call",
        action="store_true",
        help="Required acknowledgement for the external --place-call side effect.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--dashboard-port",
        type=int,
        help=(
            "With --serve-voice, also run the clinic portal in this process on "
            "the given localhost port, sharing one authoritative scheduler."
        ),
    )
    parser.add_argument(
        "--voice-log",
        default=str(PROJECT_ROOT / "data" / "voice-events.jsonl"),
        help="JSONL decision log written by --serve-voice.",
    )
    parser.add_argument(
        "--summarize-voice-log",
        action="store_true",
        help="Render the high-signal decision timeline from --voice-log.",
    )
    parser.add_argument(
        "--evaluate-voice-log",
        action="store_true",
        help=(
            "Run the deterministic transcript and structured-event quality gate. "
            "Returns exit status 1 for a hard failure."
        ),
    )
    parser.add_argument(
        "--evaluation-format",
        choices=("text", "json"),
        default="text",
        help="Output format for --evaluate-voice-log.",
    )
    parser.add_argument(
        "--latest-call",
        action="store_true",
        help=(
            "With --summarize-voice-log or --evaluate-voice-log, scope output "
            "to the newest call instead of aggregating the whole JSONL file."
        ),
    )
    parser.add_argument(
        "--database",
        default=str(PROJECT_ROOT / "data" / "clinic.db"),
        help="SQLite clinic database path.",
    )
    parser.add_argument(
        "--conversations",
        default=str(PROJECT_ROOT / "data" / "conversations.json"),
        help="TinyDB conversation document-store path.",
    )
    args = parser.parse_args(argv)

    # One deterministic configuration source for every CLI path. Explicit
    # process values win, which keeps CI and deployment overrides predictable.
    from clinic_agent.runtime.config import load_project_environment

    load_project_environment()

    if args.mock_call:
        from clinic_agent.runtime.mock_call import render_mock_call, run_mock_booking_call

        print(render_mock_call(run_mock_booking_call()))
        return 0

    if args.summarize_voice_log:
        from clinic_agent.runtime.voice_log import (
            latest_voice_call,
            load_voice_events,
            render_voice_timeline,
        )

        try:
            events = load_voice_events(args.voice_log)
            if args.latest_call:
                events = latest_voice_call(events)
            print(render_voice_timeline(events))
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        return 0

    if args.evaluate_voice_log:
        from clinic_agent.runtime.transcript_evaluator import (
            evaluate_voice_events,
            render_voice_evaluation,
        )
        from clinic_agent.runtime.voice_log import latest_voice_call, load_voice_events

        try:
            events = load_voice_events(args.voice_log)
            if args.latest_call:
                events = latest_voice_call(events)
            result = evaluate_voice_events(events)
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        if args.evaluation_format == "json":
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            print(render_voice_evaluation(result))
        return result.exit_code

    if args.check_voice_config:
        from clinic_agent.runtime.config import voice_configuration_status

        status = voice_configuration_status()
        for name, present in status.items():
            print(f"{name}={'set' if present else 'missing'}")
        return 0 if all(status.values()) else 2

    if args.save_voice_config:
        from clinic_agent.runtime.config import persist_voice_environment

        try:
            destination = persist_voice_environment()
        except ValueError as error:
            parser.error(str(error))
        print(f"Voice configuration saved to {destination} (values not displayed).")
        return 0

    if args.serve_voice:
        from clinic_agent.runtime.config import VoiceRuntimeSettings
        from clinic_agent.runtime.outbound_call import place_outbound_call
        from clinic_agent.runtime.voice_server import create_configured_voice_app
        from appointment_harness.web import build_server
        from twilio.rest import Client
        import uvicorn

        try:
            settings = VoiceRuntimeSettings.from_environment()
        except ValueError as error:
            parser.error(str(error))
        app = create_configured_voice_app(
            settings,
            database_path=args.database,
            event_log_path=args.voice_log,
        )
        dashboard = None
        dashboard_thread = None
        if args.dashboard_port is not None:
            if args.host not in {"127.0.0.1", "localhost", "::1"}:
                parser.error("the clinic portal may only bind to localhost")
            if args.dashboard_port == args.port:
                parser.error("--dashboard-port must differ from --port")
            client = Client(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
            )

            def dispatch(notification_id: str, destination: str) -> str:
                placed = place_outbound_call(
                    destination,
                    settings,
                    client=client,
                    context_reference=notification_id,
                )
                return placed.sid

            dashboard = build_server(
                harness=app.state.clinic_harness,
                repository=app.state.clinic_repository,
                conversation_path=Path(args.conversations).resolve(),
                host=args.host,
                port=args.dashboard_port,
                confirmation_dispatcher=dispatch,
            )
            dashboard_thread = threading.Thread(
                target=dashboard.serve_forever,
                name="clinic-dashboard",
                daemon=True,
            )
            dashboard_thread.start()
            print(f"Clinic dashboard: http://{args.host}:{args.dashboard_port}")
        try:
            uvicorn.run(app, host=args.host, port=args.port)
        finally:
            if dashboard is not None:
                dashboard.shutdown()
                dashboard.server_close()
            if dashboard_thread is not None:
                dashboard_thread.join(timeout=2)
        return 0

    if args.check_twilio_account:
        from clinic_agent.runtime.config import VoiceRuntimeSettings
        from clinic_agent.runtime.twilio_account import probe_twilio_account
        from twilio.base.exceptions import TwilioRestException
        from twilio.rest import Client

        try:
            settings = VoiceRuntimeSettings.from_environment()
            status = probe_twilio_account(
                settings,
                client=Client(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                ),
            )
        except ValueError as error:
            parser.error(str(error))
        except TwilioRestException as error:
            parser.error(
                f"Twilio capability check failed (HTTP {error.status}, code {error.code})"
            )
        print(f"account_status={status.account_status}")
        print(f"account_type={status.account_type}")
        print(f"balance={status.balance} {status.currency}")
        print(f"configured_number_found={str(status.configured_number_found).lower()}")
        print(
            "configured_number_voice_capable="
            f"{str(status.configured_number_voice_capable).lower()}"
        )
        return 0

    if args.place_call:
        if not args.yes_place_real_call:
            parser.error(
                "--place-call makes a real external call; rerun with "
                "--yes-place-real-call after checking the destination"
            )
        from clinic_agent.runtime.config import VoiceRuntimeSettings
        from clinic_agent.runtime.outbound_call import (
            explain_twilio_call_error,
            place_outbound_call,
        )
        from twilio.base.exceptions import TwilioRestException
        from twilio.rest import Client

        try:
            settings = VoiceRuntimeSettings.from_environment()
            result = place_outbound_call(
                args.place_call,
                settings,
                client=Client(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                ),
            )
        except (ValueError, RuntimeError) as error:
            parser.error(str(error))
        except TwilioRestException as error:
            parser.error(
                explain_twilio_call_error(status=error.status, code=error.code)
            )
        print(f"Twilio call created: sid={result.sid} status={result.status}")
        return 0

    if not os.environ.get("OPENAI_API_KEY"):
        parser.error(
            "OPENAI_API_KEY is not available in this terminal. Set it for this "
            "PowerShell session and rerun the command."
        )

    from openai import OpenAI

    harness, repository = open_sqlite_harness(args.database)
    store, clock = harness.store, harness.clock
    try:
        conversations: ConversationDocumentStore | None = (
            ConversationDocumentStore(args.conversations)
        )
    except Exception as error:
        conversations = None
        print(
            "[conversation-store warning] Conversation persistence is unavailable; "
            f"scheduling remains active ({type(error).__name__})."
        )
    session_id = f"text-{uuid4().hex[:12]}"
    patient_id = "patient-001"
    patient = store.patients[patient_id]
    state_machine = ConversationStateMachine(
        patient_display_name=patient.display_name
    )

    runtime = GuardedToolRuntime(
        harness,
        session_id=session_id,
        patient_id=patient_id,
        state_machine=state_machine,
    )
    session = ResponsesTextSession(
        OpenAI(),
        runtime,
        instructions=text_runtime_instructions(
            build_live_prompt(_context()), current_time=clock.now()
        ),
        model=args.model,
        state_machine=state_machine,
    )

    print(
        f"\nMira: {state_machine.greeting(assistant_name='Mira', clinic_name=CLINIC_NAME)}"
    )
    print("Type 'quit' to end. For a pending proposal, reply exactly 'yes, confirm'.")
    if conversations is not None:
        _record_conversation_event(
            lambda: conversations.append_turn(
                session_id=session_id,
                role="assistant",
                content=state_machine.greeting(
                    assistant_name="Mira", clinic_name=CLINIC_NAME
                ),
                occurred_at=clock.now(),
                metadata={"workflow_state": state_machine.state.value},
            ),
            label="greeting",
        )
    repository.upsert_session_state(
        session_id=session_id,
        patient_id=patient_id,
        identity_status="unverified",
        authority_mode=None,
        workflow_state=state_machine.state.value,
        now=clock.now(),
    )
    shown_transitions = 0
    shown_tool_events = 0
    while True:
        try:
            patient_text = input("\nPatient: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if patient_text.casefold() in {"quit", "exit"}:
            break
        if not patient_text:
            continue
        if conversations is not None:
            _record_conversation_event(
                lambda: conversations.append_turn(
                    session_id=session_id,
                    role="patient",
                    content=patient_text,
                    occurred_at=clock.now(),
                    metadata={"workflow_state": state_machine.state.value},
                ),
                label="patient turn",
            )
        try:
            directive = state_machine.accept_patient_turn(
                patient_text,
                authorize_name_confirmation=lambda confirmed: (
                    harness.confirm_identity_by_name(
                        session_id=session_id,
                        patient_id=patient_id,
                        confirmed=confirmed,
                    )
                ),
                authorize_proxy=lambda caller_name, relationship: (
                    harness.confirm_proxy_authority(
                        session_id=session_id,
                        patient_id=patient_id,
                        caller_display_name=caller_name,
                        relationship=relationship,
                    )
                ),
            )
            if directive.handoff_reason is not None:
                handoff = harness.request_general_handoff(
                    GeneralHandoffCommand(
                        session_id=session_id,
                        patient_id=patient_id,
                        reason=directive.handoff_reason,
                        topics=("caller_requested_assistance",),
                        idempotency_key=(
                            f"{session_id}:handoff:{len(state_machine.transitions)}"
                        ),
                    )
                )
                accepted = handoff.outcome.value in {"accepted", "pending"}
                state_machine.handoff_resolved(accepted=accepted)
                if handoff.outcome.value == "accepted":
                    reply = (
                        "I've sent a request to the front desk. Your handoff "
                        f"reference is {handoff.handoff_id}."
                    )
                elif handoff.outcome.value == "pending":
                    reply = (
                        "The front-desk request is pending. Your handoff reference "
                        f"is {handoff.handoff_id}."
                    )
                else:
                    reply = (
                        "I couldn't create the front-desk request. Please call the "
                        "clinic directly."
                    )
            elif directive.reply is not None:
                reply = directive.reply
            elif directive.model_input is not None:
                reply = session.respond(directive.model_input)
            else:
                raise RuntimeError("state machine produced no turn directive")
        except Exception as error:
            print(f"\nSession error: {type(error).__name__}: {error}")
            return 1
        print(f"\nMira: {reply}")
        if conversations is not None:
            _record_conversation_event(
                lambda: conversations.append_turn(
                    session_id=session_id,
                    role="assistant",
                    content=reply,
                    occurred_at=clock.now(),
                    metadata={"workflow_state": state_machine.state.value},
                ),
                label="assistant turn",
            )
        for transition in state_machine.transitions[shown_transitions:]:
            print(
                f"[state] {transition.previous.value} -> "
                f"{transition.current.value} ({transition.reason})"
            )
            if conversations is not None:
                _record_conversation_event(
                    lambda transition=transition: conversations.append_event(
                        session_id=session_id,
                        event_type="workflow.transition",
                        occurred_at=clock.now(),
                        payload={
                            "previous": transition.previous.value,
                            "current": transition.current.value,
                            "reason": transition.reason,
                        },
                    ),
                    label="state transition",
                )
        shown_transitions = len(state_machine.transitions)
        for event in runtime.trace[shown_tool_events:]:
            if event.get("event") == "tool.result":
                print(f"[trace] {event['tool']} -> {event['status']}")
            elif event.get("event") == "proposal.invalidated":
                print(
                    f"[trace] proposal {event['proposal_id']} invalidated "
                    f"({event['reason']})"
                )
            if conversations is not None:
                _record_conversation_event(
                    lambda event=event: conversations.append_event(
                        session_id=session_id,
                        event_type=str(event.get("event", "runtime.event")),
                        occurred_at=clock.now(),
                        payload=event,
                    ),
                    label="runtime event",
                )
        shown_tool_events = len(runtime.trace)
        repository.upsert_session_state(
            session_id=session_id,
            patient_id=patient_id,
            identity_status=(
                "verified" if state_machine.authority_mode else "unverified"
            ),
            authority_mode=state_machine.authority_mode,
            workflow_state=state_machine.state.value,
            now=clock.now(),
        )
    if conversations is not None:
        _record_conversation_event(conversations.close, label="store close")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
