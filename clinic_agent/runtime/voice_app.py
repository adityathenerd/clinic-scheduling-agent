"""FastAPI boundary for Twilio webhooks and full-duplex media sessions.

Provider message parsing is intentionally injected.  This module verifies the
HTTP request, emits privacy-minimal TwiML, and binds each parsed media connection
to exactly one call actor without depending on Twilio's wire-message shapes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
import inspect
import secrets
from typing import Any, Awaitable, Mapping, Protocol, cast
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from clinic_agent.call_mechanics import CallDescriptor, CallSessionActor, SessionEvent
from clinic_agent.call_mechanics.actor import SessionClosedError
from clinic_agent.call_mechanics.protocols import TelephonyAdapter

from .config import VoiceRuntimeSettings
from .outbound_call import TWILIO_VOICE_WEBHOOK_PATH, public_voice_webhook_url


TWILIO_MEDIA_WEBSOCKET_PATH = "/twilio/media"


class SignatureVerifier(Protocol):
    def validate(
        self,
        url: str,
        params: Mapping[str, str],
        signature: str,
    ) -> bool: ...


class ProviderMediaSession(TelephonyAdapter, Protocol):
    """A started provider stream with normalized events and telephony writes."""

    descriptor: CallDescriptor

    async def receive_event(self) -> SessionEvent | None:
        """Return the next normalized event, or ``None`` at end of stream."""


class SessionActor(Protocol):
    descriptor: CallDescriptor
    done: asyncio.Event

    async def start(self) -> Any: ...

    async def dispatch(self, event: SessionEvent) -> Any: ...

    async def close(self, reason: str = "requested") -> None: ...


MediaSessionFactory = Callable[
    [WebSocket],
    ProviderMediaSession | Awaitable[ProviderMediaSession],
]
ActorFactory = Callable[[CallDescriptor, TelephonyAdapter], CallSessionActor]


class MediaBoundCallSupervisor:
    """Deduplicate media connections and own one actor per provider call ID."""

    def __init__(self, actor_factory: ActorFactory) -> None:
        self._actor_factory = actor_factory
        self._actors: dict[str, SessionActor] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._actors)

    async def accept(
        self, session: ProviderMediaSession
    ) -> tuple[SessionActor, bool]:
        call_id = session.descriptor.provider_call_id
        async with self._lock:
            existing = self._actors.get(call_id)
            if existing is not None:
                if existing.descriptor != session.descriptor:
                    raise ValueError(
                        "duplicate provider_call_id has conflicting call metadata"
                    )
                return existing, False

            actor = cast(
                SessionActor,
                self._actor_factory(session.descriptor, session),
            )
            self._actors[call_id] = actor

        try:
            await actor.start()
        except Exception:
            async with self._lock:
                if self._actors.get(call_id) is actor:
                    self._actors.pop(call_id, None)
            raise

        asyncio.create_task(
            self._remove_when_done(call_id, actor),
            name=f"media-supervisor:{call_id}",
        )
        return actor, True

    async def _remove_when_done(self, call_id: str, actor: SessionActor) -> None:
        await actor.done.wait()
        async with self._lock:
            if self._actors.get(call_id) is actor:
                self._actors.pop(call_id, None)

    async def close_all(self, reason: str = "service_shutdown") -> None:
        async with self._lock:
            actors = list(self._actors.values())
        await asyncio.gather(
            *(actor.close(reason) for actor in actors),
            return_exceptions=True,
        )


def public_media_websocket_url(public_base_url: str) -> str:
    """Convert the configured HTTPS origin to its exact WSS media endpoint."""

    parts = urlsplit(public_base_url.rstrip("/"))
    if parts.scheme != "https" or not parts.netloc:
        raise ValueError("PUBLIC_BASE_URL must be an absolute https:// URL")
    path = parts.path.rstrip("/") + TWILIO_MEDIA_WEBSOCKET_PATH
    return urlunsplit(("wss", parts.netloc, path, "", ""))


def _parse_form_body(body: bytes) -> dict[str, str]:
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid form encoding") from exc
    return dict(parse_qsl(decoded, keep_blank_values=True, strict_parsing=False))


async def _resolve_media_session(
    factory: MediaSessionFactory,
    websocket: WebSocket,
) -> ProviderMediaSession:
    value = factory(websocket)
    if inspect.isawaitable(value):
        return await value
    return value


def create_voice_app(
    settings: VoiceRuntimeSettings,
    *,
    signature_verifier: SignatureVerifier | None = None,
    media_session_factory: MediaSessionFactory | None = None,
    actor_factory: ActorFactory | None = None,
    session_id_factory: Callable[[], str] | None = None,
) -> FastAPI:
    """Create the voice ingress application with explicit provider dependencies."""

    verifier = signature_verifier or RequestValidator(settings.twilio_auth_token)
    make_session_id = session_id_factory or (lambda: secrets.token_urlsafe(24))
    supervisor = MediaBoundCallSupervisor(actor_factory) if actor_factory else None

    if (media_session_factory is None) != (actor_factory is None):
        raise ValueError(
            "media_session_factory and actor_factory must be configured together"
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if supervisor is not None:
            await supervisor.close_all("service_shutdown")

    app = FastAPI(lifespan=lifespan)
    app.state.voice_supervisor = supervisor

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(TWILIO_VOICE_WEBHOOK_PATH)
    async def twilio_voice_webhook(request: Request) -> Response:
        body = await request.body()
        params = _parse_form_body(body)
        signature = request.headers.get("X-Twilio-Signature", "")
        context_reference = request.query_params.get("context_reference")
        try:
            exact_public_url = public_voice_webhook_url(
                settings.public_base_url,
                context_reference=context_reference,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        if not signature or not verifier.validate(
            exact_public_url,
            params,
            signature,
        ):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")

        if not params.get("CallSid", "").strip():
            raise HTTPException(status_code=400, detail="CallSid is required")

        response = VoiceResponse()
        stream = response.connect().stream(
            url=public_media_websocket_url(settings.public_base_url)
        )
        # Never place patient, destination, or caller data in stream parameters.
        stream.parameter(name="session_id", value=make_session_id())
        provider_direction = params.get("Direction", "").casefold()
        stream.parameter(
            name="direction",
            value=("outbound" if provider_direction.startswith("outbound") else "inbound"),
        )
        if context_reference:
            stream.parameter(name="context_reference", value=context_reference)
        return Response(content=str(response), media_type="application/xml")

    @app.websocket(TWILIO_MEDIA_WEBSOCKET_PATH)
    async def twilio_media_websocket(websocket: WebSocket) -> None:
        websocket_signature = websocket.headers.get("X-Twilio-Signature", "")
        exact_public_url = public_media_websocket_url(settings.public_base_url)
        websocket_params = dict(websocket.query_params)
        if not websocket_signature or not verifier.validate(
            exact_public_url,
            websocket_params,
            websocket_signature,
        ):
            # Reject before accepting the upgrade: an untrusted stream must never
            # allocate an actor or reach a provider parser.
            await websocket.close(code=1008, reason="invalid Twilio signature")
            return

        await websocket.accept()
        if media_session_factory is None or supervisor is None:
            await websocket.close(code=1011, reason="voice runtime is not configured")
            return

        session: ProviderMediaSession | None = None
        actor: SessionActor | None = None
        owns_actor = False
        try:
            session = await _resolve_media_session(media_session_factory, websocket)
            actor, owns_actor = await supervisor.accept(session)
            if not owns_actor:
                await session.close("duplicate_media_connection")
                return

            while True:
                event = await session.receive_event()
                if event is None:
                    break
                try:
                    await actor.dispatch(event)
                except SessionClosedError:
                    break
                if event.kind == "telephony.stop" or actor.done.is_set():
                    break
        except WebSocketDisconnect:
            pass
        except (ValueError, TypeError):
            if actor is not None and not actor.done.is_set():
                try:
                    await actor.dispatch(
                        SessionEvent(
                            "voice.error",
                            {"reason": "invalid_media_event"},
                        )
                    )
                except SessionClosedError:
                    pass
            elif session is None:
                await websocket.close(code=1008, reason="invalid media session")
            else:
                await session.close("invalid_media_event")
        finally:
            if owns_actor and actor is not None and not actor.done.is_set():
                await actor.close("media_websocket_closed")
            try:
                await websocket.close(code=1000)
            except RuntimeError:
                # The peer may already have completed the close handshake.
                pass

    return app


__all__ = [
    "ActorFactory",
    "MediaBoundCallSupervisor",
    "MediaSessionFactory",
    "ProviderMediaSession",
    "SignatureVerifier",
    "TWILIO_MEDIA_WEBSOCKET_PATH",
    "create_voice_app",
    "public_media_websocket_url",
]
