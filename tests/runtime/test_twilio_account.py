from __future__ import annotations

from types import SimpleNamespace
import unittest

from clinic_agent.runtime.config import VoiceRuntimeSettings
from clinic_agent.runtime.twilio_account import probe_twilio_account


class Resource:
    def __init__(self, value: object) -> None:
        self.value = value
        self.calls: list[dict[str, object]] = []

    def fetch(self) -> object:
        return self.value

    def list(self, **kwargs: object) -> list[object]:
        self.calls.append(kwargs)
        return list(self.value)  # type: ignore[arg-type]


class Accounts:
    def __init__(self, value: object) -> None:
        self.value = value
        self.requested: list[str] = []

    def __call__(self, sid: str) -> Resource:
        self.requested.append(sid)
        return Resource(self.value)


class TwilioAccountProbeTests(unittest.TestCase):
    def test_probe_is_read_only_and_reports_configured_voice_number(self) -> None:
        accounts = Accounts(SimpleNamespace(status="active", type="Trial"))
        numbers = Resource([SimpleNamespace(capabilities={"voice": True})])
        client = SimpleNamespace(
            api=SimpleNamespace(accounts=accounts),
            balance=Resource(SimpleNamespace(balance="10.3105", currency="USD")),
            incoming_phone_numbers=numbers,
        )
        settings = VoiceRuntimeSettings(
            openai_api_key="openai",
            twilio_account_sid="AC123",
            twilio_auth_token="token",
            twilio_phone_number="+14155552671",
            public_base_url="https://voice.example.test",
        )

        result = probe_twilio_account(settings, client=client)

        self.assertEqual("Trial", result.account_type)
        self.assertEqual("10.3105", result.balance)
        self.assertTrue(result.configured_number_found)
        self.assertTrue(result.configured_number_voice_capable)
        self.assertEqual(["AC123"], accounts.requested)
        self.assertEqual(
            [{"phone_number": "+14155552671", "limit": 1}], numbers.calls
        )


if __name__ == "__main__":
    unittest.main()
