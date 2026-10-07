"""Explicit Twilio side effect for starting an outbound phone call.

The helper deliberately accepts an already-constructed client.  Credential loading
and the decision to place a real call remain at the composition/CLI boundary, while
this module owns validation and the small provider request/response contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol
from urllib.parse import urlencode

from .config import VoiceRuntimeSettings


TWILIO_VOICE_WEBHOOK_PATH = "/twilio/voice"
_E164_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")


def explain_twilio_call_error(*, status: int | None, code: int | None) -> str:
    """Return a safe, actionable provider error without echoing phone numbers."""

    if code == 21219:
        return (
            "Twilio trial rejected the destination because it is not a verified "
            "Caller ID (HTTP 400, code 21219). Verify the destination in the "
            "Twilio Console under Phone Numbers > Manage > Verified Caller IDs, "
            "or call the account's existing verified destination."
        )
    return f"Twilio call creation failed (HTTP {status}, code {code})"


class _CallsResource(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class TwilioClient(Protocol):
    calls: _CallsResource


@dataclass(frozen=True, slots=True)
class OutboundCallResult:
    """Minimal non-sensitive result safe to print or persist."""

    sid: str
    status: str


def validate_e164(value: str, *, field_name: str = "phone number") -> str:
    """Return a normalized E.164 value or reject it at the boundary."""

    normalized = value.strip()
    if not _E164_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must be E.164 formatted (for example +14155552671)"
        )
    return normalized


def public_voice_webhook_url(
    public_base_url: str, *, context_reference: str | None = None
) -> str:
    """Build the same canonical URL used for Twilio signature verification."""

    base = public_base_url.rstrip("/") + TWILIO_VOICE_WEBHOOK_PATH
    if context_reference is None:
        return base
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", context_reference):
        raise ValueError("context_reference must be an opaque identifier")
    return base + "?" + urlencode({"context_reference": context_reference})


def place_outbound_call(
    destination: str,
    settings: VoiceRuntimeSettings,
    *,
    client: TwilioClient,
    source: str | None = None,
    context_reference: str | None = None,
) -> OutboundCallResult:
    """Place one real outbound call and return only its SID and provider status.

    Calling this function is the external side effect.  It never constructs a
    client implicitly, so callers must load credentials and opt into the provider
    operation before reaching this boundary.
    """

    to_number = validate_e164(destination, field_name="destination")
    from_number = validate_e164(
        settings.twilio_phone_number if source is None else source,
        field_name="source",
    )
    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=public_voice_webhook_url(
            settings.public_base_url, context_reference=context_reference
        ),
        method="POST",
    )
    sid = str(getattr(call, "sid", "")).strip()
    if not sid:
        raise RuntimeError("Twilio returned a call without a SID")
    status = str(getattr(call, "status", "unknown") or "unknown")
    return OutboundCallResult(sid=sid, status=status)
