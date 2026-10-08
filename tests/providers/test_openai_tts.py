from __future__ import annotations

import math
import struct
from types import SimpleNamespace
import unittest

from clinic_agent.providers.openai_tts import OpenAISpeechRenderer, pcm24_to_pcmu8


class OpenAISpeechRendererTests(unittest.TestCase):
    def test_pcm24_to_pcmu8_produces_eight_khz_mulaw_duration(self) -> None:
        samples = [
            int(8_000 * math.sin(2 * math.pi * 440 * index / 24_000))
            for index in range(2_400)
        ]
        pcm = b"".join(struct.pack("<h", sample) for sample in samples)

        pcmu = pcm24_to_pcmu8(pcm)

        self.assertEqual(800, len(pcmu))

    def test_pcm_conversion_rejects_incomplete_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "complete 16-bit samples"):
            pcm24_to_pcmu8(b"\x00")


class OpenAISpeechRendererContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_pcm_and_returns_telephony_ready_clip(self) -> None:
        pcm = b"\x00\x00" * 2_400

        class Speech:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            async def create(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return SimpleNamespace(content=pcm)

        speech = Speech()
        client = SimpleNamespace(audio=SimpleNamespace(speech=speech))
        renderer = OpenAISpeechRenderer(client)  # type: ignore[arg-type]

        clip = await renderer.render("Identity accepted.")

        self.assertEqual(800, len(clip.payload))
        self.assertEqual(100, clip.duration_ms)
        self.assertEqual("pcm", speech.calls[0]["response_format"])
        self.assertEqual("audio", speech.calls[0]["stream_format"])
        self.assertEqual("gpt-4o-mini-tts", speech.calls[0]["model"])
        self.assertEqual("marin", speech.calls[0]["voice"])

        cached = await renderer.render("Identity accepted.")
        self.assertIs(clip, cached)
        self.assertEqual(1, len(speech.calls))


if __name__ == "__main__":
    unittest.main()
