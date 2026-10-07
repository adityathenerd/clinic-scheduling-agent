from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from clinic_agent.runtime.config import VoiceRuntimeSettings
from clinic_agent.runtime.voice_server import create_configured_voice_app


class VoiceServerCompositionTests(unittest.TestCase):
    @staticmethod
    def _settings() -> VoiceRuntimeSettings:
        return VoiceRuntimeSettings(
            openai_api_key="test-openai",
            twilio_account_sid="ACtest",
            twilio_auth_token="test-token",
            twilio_phone_number="+14155552671",
            public_base_url="https://voice.example.test",
        )

    def test_composed_app_is_healthy_without_opening_provider_connections(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_configured_voice_app(
                self._settings(),
                database_path=Path(directory) / "clinic.db",
            )
            with TestClient(app) as client:
                response = client.get("/healthz")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())
        self.assertIsNotNone(app.state.clinic_harness)

    def test_demo_patient_destination_is_validated_and_persisted_from_environment(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"DEMO_PATIENT_PHONE_E164": "+14155550123"}
        ):
            app = create_configured_voice_app(
                self._settings(),
                database_path=Path(directory) / "clinic.db",
            )

        self.assertEqual(
            "+14155550123",
            app.state.clinic_harness.store.patients["patient-001"].phone_e164,
        )

    def test_invalid_demo_patient_destination_fails_before_server_start(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"DEMO_PATIENT_PHONE_E164": "not-e164"}
        ):
            with self.assertRaisesRegex(ValueError, "DEMO_PATIENT_PHONE_E164"):
                create_configured_voice_app(
                    self._settings(),
                    database_path=Path(directory) / "clinic.db",
                )


if __name__ == "__main__":
    unittest.main()
