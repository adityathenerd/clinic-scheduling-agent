from __future__ import annotations

from types import SimpleNamespace
import unittest

from clinic_agent.runtime.config import VoiceRuntimeSettings
from clinic_agent.runtime.outbound_call import (
    OutboundCallResult,
    explain_twilio_call_error,
    place_outbound_call,
    public_voice_webhook_url,
    validate_e164,
)


class _FakeCalls:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.response = SimpleNamespace(sid="CA123", status="queued")

    def create(self, **kwargs: object) -> object:
        self.requests.append(kwargs)
        return self.response


class _FakeClient:
    def __init__(self) -> None:
        self.calls = _FakeCalls()


def _settings() -> VoiceRuntimeSettings:
    return VoiceRuntimeSettings(
        openai_api_key="not-a-real-key",
        twilio_account_sid="AC-not-real",
        twilio_auth_token="not-a-real-token",
        twilio_phone_number="+14155550100",
        public_base_url="https://voice.example.test/base",
    )


class OutboundCallTests(unittest.TestCase):
    def test_trial_unverified_destination_error_is_actionable_and_private(self) -> None:
        message = explain_twilio_call_error(status=400, code=21219)

        self.assertIn("not a verified Caller ID", message)
        self.assertIn("Verified Caller IDs", message)
        self.assertNotIn("+14155550199", message)

    def test_unknown_twilio_error_retains_status_and_code(self) -> None:
        self.assertEqual(
            "Twilio call creation failed (HTTP 503, code 99999)",
            explain_twilio_call_error(status=503, code=99999),
        )

    def test_places_one_explicit_call_with_canonical_callback(self) -> None:
        client = _FakeClient()

        result = place_outbound_call(
            "+14155550199",
            _settings(),
            client=client,
        )

        self.assertEqual(OutboundCallResult("CA123", "queued"), result)
        self.assertEqual(
            [
                {
                    "to": "+14155550199",
                    "from_": "+14155550100",
                    "url": "https://voice.example.test/base/twilio/voice",
                    "method": "POST",
                }
            ],
            client.calls.requests,
        )

    def test_confirmation_call_carries_only_opaque_context_reference(self) -> None:
        client = _FakeClient()

        place_outbound_call(
            "+14155550199",
            _settings(),
            client=client,
            context_reference="confirmation-call-0001",
        )

        self.assertEqual(
            "https://voice.example.test/base/twilio/voice?context_reference=confirmation-call-0001",
            client.calls.requests[0]["url"],
        )
        self.assertNotIn("patient", str(client.calls.requests[0]["url"]))

    def test_allows_an_explicit_source_override(self) -> None:
        client = _FakeClient()
        place_outbound_call(
            "+442071838750",
            _settings(),
            client=client,
            source="+442079460000",
        )
        self.assertEqual("+442079460000", client.calls.requests[0]["from_"])

    def test_rejects_non_e164_destination_before_provider_side_effect(self) -> None:
        client = _FakeClient()
        with self.assertRaisesRegex(ValueError, "destination must be E.164"):
            place_outbound_call("415-555-0199", _settings(), client=client)
        self.assertEqual([], client.calls.requests)

    def test_rejects_invalid_source_before_provider_side_effect(self) -> None:
        client = _FakeClient()
        with self.assertRaisesRegex(ValueError, "source must be E.164"):
            place_outbound_call(
                "+14155550199",
                _settings(),
                client=client,
                source="0014155550100",
            )
        self.assertEqual([], client.calls.requests)

    def test_requires_provider_sid(self) -> None:
        client = _FakeClient()
        client.calls.response = SimpleNamespace(sid="", status=None)
        with self.assertRaisesRegex(RuntimeError, "without a SID"):
            place_outbound_call("+14155550199", _settings(), client=client)

    def test_url_and_e164_helpers_are_deterministic(self) -> None:
        self.assertEqual("+14155550199", validate_e164(" +14155550199 "))
        self.assertEqual(
            "https://voice.example.test/twilio/voice",
            public_voice_webhook_url("https://voice.example.test/"),
        )
        self.assertEqual(
            "https://voice.example.test/twilio/voice?context_reference=job_123",
            public_voice_webhook_url(
                "https://voice.example.test/", context_reference="job_123"
            ),
        )


if __name__ == "__main__":
    unittest.main()
