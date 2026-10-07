from __future__ import annotations

import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from dotenv import dotenv_values

from clinic_agent.runtime.config import (
    VoiceRuntimeSettings,
    load_project_environment,
    persist_voice_environment,
    voice_configuration_status,
)


class VoiceRuntimeSettingsTests(unittest.TestCase):
    def test_project_env_loads_without_overriding_process_values(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "OPENAI_API_KEY=from-file\n"
                "TWILIO_ACCOUNT_SID=from-file-sid\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "from-process"},
                clear=True,
            ):
                loaded = load_project_environment(path)
                self.assertTrue(loaded)
                self.assertEqual("from-process", os.environ["OPENAI_API_KEY"])
                self.assertEqual("from-file-sid", os.environ["TWILIO_ACCOUNT_SID"])

    def test_missing_project_env_is_a_safe_noop(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "missing.env"
            self.assertFalse(load_project_environment(path))

    def test_persist_voice_environment_writes_values_and_preserves_other_keys(self) -> None:
        environment = {
            "OPENAI_API_KEY": "secret-openai",
            "TWILIO_ACCOUNT_SID": "secret-sid",
            "TWILIO_AUTH_TOKEN": "secret-token",
            "TWILIO_PHONE_NUMBER": "+15005550006",
            "PUBLIC_BASE_URL": "https://voice.example.test",
            "VOICE_LOG_TRANSCRIPTS": "1",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("UNRELATED=value\n", encoding="utf-8")

            saved = persist_voice_environment(environment, path=path)

            values = dotenv_values(saved)
            self.assertEqual("value", values["UNRELATED"])
            for name, value in environment.items():
                self.assertEqual(value, values[name])

    def test_persist_rejects_incomplete_config_without_creating_file(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            with self.assertRaisesRegex(ValueError, "TWILIO_AUTH_TOKEN"):
                persist_voice_environment(
                    {
                        "OPENAI_API_KEY": "secret-openai",
                        "TWILIO_ACCOUNT_SID": "secret-sid",
                    },
                    path=path,
                )
            self.assertFalse(path.exists())

    def test_reports_presence_without_returning_values(self) -> None:
        environment = {
            "OPENAI_API_KEY": "secret-openai",
            "TWILIO_ACCOUNT_SID": "secret-sid",
        }

        status = voice_configuration_status(environment)

        self.assertTrue(status["OPENAI_API_KEY"])
        self.assertTrue(status["TWILIO_ACCOUNT_SID"])
        self.assertFalse(status["TWILIO_AUTH_TOKEN"])
        self.assertNotIn("secret-openai", repr(status))

    def test_rejects_incomplete_voice_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "TWILIO_AUTH_TOKEN"):
            VoiceRuntimeSettings.from_environment(
                {
                    "OPENAI_API_KEY": "secret-openai",
                    "TWILIO_ACCOUNT_SID": "secret-sid",
                }
            )

    def test_requires_https_public_base_url_and_hides_secrets_from_repr(self) -> None:
        environment = {
            "OPENAI_API_KEY": "secret-openai",
            "TWILIO_ACCOUNT_SID": "secret-sid",
            "TWILIO_AUTH_TOKEN": "secret-token",
            "TWILIO_PHONE_NUMBER": "+15005550006",
            "PUBLIC_BASE_URL": "https://voice.example.test/",
        }

        settings = VoiceRuntimeSettings.from_environment(environment)

        self.assertEqual("https://voice.example.test", settings.public_base_url)
        self.assertNotIn("secret-openai", repr(settings))
        self.assertNotIn("secret-token", repr(settings))


if __name__ == "__main__":
    unittest.main()
