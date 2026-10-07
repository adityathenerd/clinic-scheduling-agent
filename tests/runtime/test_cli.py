from __future__ import annotations

import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from clinic_agent.__main__ import main


class VoiceCliSafetyTests(unittest.TestCase):
    def test_real_call_requires_explicit_acknowledgement_flag(self) -> None:
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--place-call", "+14155552671"])
        self.assertEqual(2, raised.exception.code)

    def test_voice_log_summary_does_not_require_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "voice.jsonl"
            path.write_text(
                '{"event_type":"call.ended","occurred_at":"now","outcome":"closed"}\n',
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stderr(StringIO()), redirect_stdout(output):
                result = main(
                    [
                        "--summarize-voice-log",
                        "--voice-log",
                        str(path),
                    ]
                )
        self.assertEqual(0, result)
        self.assertIn("terminal=closed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
