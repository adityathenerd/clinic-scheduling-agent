"""Application-owned speech rendering for deterministic workflow messages."""

from __future__ import annotations

import asyncio
import audioop
from collections import OrderedDict
from typing import Any

from openai import AsyncOpenAI

from clinic_agent.call_mechanics.models import SpeechClip


PCM_SAMPLE_RATE = 24_000
TELEPHONY_SAMPLE_RATE = 8_000
MAX_SPEECH_CHARACTERS = 2_000


def pcm24_to_pcmu8(payload: bytes) -> bytes:
    """Convert mono PCM16LE/24 kHz from the Speech API to PCMU/8 kHz."""

    if not payload or len(payload) % 2:
        raise ValueError("speech PCM must contain complete 16-bit samples")
    resampled, _ = audioop.ratecv(
        payload,
        2,
        1,
        PCM_SAMPLE_RATE,
        TELEPHONY_SAMPLE_RATE,
        None,
    )
    encoded = audioop.lin2ulaw(resampled, 2)
    if not encoded:
        raise ValueError("speech conversion produced no telephony audio")
    return encoded


class OpenAISpeechRenderer:
    """Render trusted text as a complete clip before telephony playback."""

    def __init__(
        self,
        client: AsyncOpenAI,
        *,
        model: str = "gpt-4o-mini-tts",
        voice: str = "marin",
        timeout: float = 8.0,
        cache_size: int = 64,
    ) -> None:
        if timeout <= 0 or cache_size <= 0:
            raise ValueError("speech render timeout and cache size must be positive")
        self._client = client
        self._model = model
        self._voice = voice
        self._timeout = timeout
        self._cache_size = cache_size
        self._cache: OrderedDict[str, SpeechClip] = OrderedDict()
        self._cache_lock = asyncio.Lock()

    async def render(self, text: str) -> SpeechClip:
        normalized = text.strip()
        if not normalized:
            raise ValueError("speech text is required")
        if len(normalized) > MAX_SPEECH_CHARACTERS:
            raise ValueError("speech text exceeds the application-owned limit")
        async with self._cache_lock:
            cached = self._cache.get(normalized)
            if cached is not None:
                self._cache.move_to_end(normalized)
                return cached
        async with asyncio.timeout(self._timeout):
            response: Any = await self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=normalized,
                instructions=(
                    "Speak clearly, warmly, and concisely as Mira, the scheduling "
                    "assistant for 2care Clinic. Preserve every word exactly."
                ),
                response_format="pcm",
                stream_format="audio",
                timeout=self._timeout,
            )
        pcmu = pcm24_to_pcmu8(bytes(response.content))
        clip = SpeechClip(payload=pcmu, duration_ms=max(1, len(pcmu) // 8))
        async with self._cache_lock:
            self._cache[normalized] = clip
            self._cache.move_to_end(normalized)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return clip


__all__ = ["OpenAISpeechRenderer", "pcm24_to_pcmu8"]
