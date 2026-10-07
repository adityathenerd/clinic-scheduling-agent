"""Read-only Twilio account capability probe used before a live call."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from .config import VoiceRuntimeSettings


class TwilioAccountClient(Protocol):
    api: Any
    balance: Any
    incoming_phone_numbers: Any


@dataclass(frozen=True, slots=True)
class TwilioAccountStatus:
    account_status: str
    account_type: str
    balance: str
    currency: str
    configured_number_found: bool
    configured_number_voice_capable: bool


def probe_twilio_account(
    settings: VoiceRuntimeSettings,
    *,
    client: TwilioAccountClient,
) -> TwilioAccountStatus:
    """Fetch account, balance, and configured-number capability without writes."""

    account = client.api.accounts(settings.twilio_account_sid).fetch()
    balance = client.balance.fetch()
    numbers = client.incoming_phone_numbers.list(
        phone_number=settings.twilio_phone_number,
        limit=1,
    )
    number = numbers[0] if numbers else None
    capabilities = getattr(number, "capabilities", {}) if number is not None else {}
    if not isinstance(capabilities, dict):
        capabilities = dict(capabilities or {})
    raw_balance = getattr(balance, "balance", "unknown")
    try:
        normalized_balance = format(Decimal(str(raw_balance)), "f")
    except Exception:
        normalized_balance = "unknown"
    return TwilioAccountStatus(
        account_status=str(getattr(account, "status", "unknown")),
        account_type=str(getattr(account, "type", "unknown")),
        balance=normalized_balance,
        currency=str(getattr(balance, "currency", "unknown")),
        configured_number_found=number is not None,
        configured_number_voice_capable=bool(capabilities.get("voice", False)),
    )
