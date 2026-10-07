"""Credential-safe project-local runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv, set_key


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ENV_FILE = PROJECT_ROOT / ".env"


VOICE_ENVIRONMENT_KEYS = (
    "OPENAI_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "PUBLIC_BASE_URL",
)

LOCAL_ENVIRONMENT_KEYS = VOICE_ENVIRONMENT_KEYS + (
    "VOICE_LOG_TRANSCRIPTS",
    "DEMO_PATIENT_PHONE_E164",
)


def load_project_environment(path: str | Path = PROJECT_ENV_FILE) -> bool:
    """Load one fixed project-root .env without overriding process values."""

    source = Path(path).resolve()
    if not source.is_file():
        return False
    return bool(load_dotenv(dotenv_path=source, override=False))


def persist_voice_environment(
    environment: Mapping[str, str] | None = None,
    *,
    path: str | Path = PROJECT_ENV_FILE,
) -> Path:
    """Persist validated process configuration without printing secret values.

    This is an explicit migration operation for a credential-bearing shell. It
    owns only the voice runtime keys and preserves unrelated entries already in
    the file. Newline-bearing values are rejected to prevent line injection.
    """

    source = os.environ if environment is None else environment
    VoiceRuntimeSettings.from_environment(source)
    values = {
        name: source.get(name, "").strip()
        for name in LOCAL_ENVIRONMENT_KEYS
        if source.get(name, "").strip()
    }
    invalid = [name for name, value in values.items() if "\n" in value or "\r" in value]
    if invalid:
        raise ValueError(
            "Environment values may not contain newlines: " + ", ".join(invalid)
        )

    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_text(
            "# Local voice runtime configuration. Never commit this file.\n",
            encoding="utf-8",
        )
    for name, value in values.items():
        set_key(str(destination), name, value, quote_mode="always")
    return destination


@dataclass(frozen=True, slots=True)
class VoiceRuntimeSettings:
    openai_api_key: str = field(repr=False)
    twilio_account_sid: str = field(repr=False)
    twilio_auth_token: str = field(repr=False)
    twilio_phone_number: str
    public_base_url: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "VoiceRuntimeSettings":
        source = os.environ if environment is None else environment
        values = {name: source.get(name, "").strip() for name in VOICE_ENVIRONMENT_KEYS}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(
                "Missing voice runtime configuration: " + ", ".join(missing)
            )
        if not values["PUBLIC_BASE_URL"].startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use https://")
        return cls(
            openai_api_key=values["OPENAI_API_KEY"],
            twilio_account_sid=values["TWILIO_ACCOUNT_SID"],
            twilio_auth_token=values["TWILIO_AUTH_TOKEN"],
            twilio_phone_number=values["TWILIO_PHONE_NUMBER"],
            public_base_url=values["PUBLIC_BASE_URL"].rstrip("/"),
        )


def voice_configuration_status(
    environment: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Return presence flags only; never return credential values."""

    source = os.environ if environment is None else environment
    return {name: bool(source.get(name, "").strip()) for name in VOICE_ENVIRONMENT_KEYS}
