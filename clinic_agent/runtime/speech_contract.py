"""Canonical hashes for application-owned spoken replies.

The raw reply can contain patient-facing details, so logs retain only a digest.
The evaluator applies the same normalization to the observed transcript.
"""

from __future__ import annotations

from hashlib import sha256
import re


def normalize_spoken_contract(value: str) -> str:
    """Normalize harmless transcript formatting without changing word content."""

    without_annotations = re.sub(r"\[[^\]]*\]", " ", value.casefold())
    # Speech transcription verbalizes symbols and ordinals that are written in
    # application-rendered timestamps. Keep these equivalences deliberately
    # narrow so substantive wording changes still fail the exact-reply gate.
    spoken_time_equivalents = re.sub(
        r"\butc\s+(?:plus|minus)\s+(?=\d)",
        "utc ",
        without_annotations,
    )
    spoken_time_equivalents = re.sub(
        r"\b(\d+)(?:st|nd|rd|th)\b",
        r"\1",
        spoken_time_equivalents,
    )
    spoken_time_equivalents = re.sub(
        r"\b(\d{1,2}):00\s+(am|pm)\b",
        r"\1 \2",
        spoken_time_equivalents,
    )
    return " ".join(re.findall(r"[a-z0-9]+", spoken_time_equivalents))


def spoken_contract_digest(value: str) -> str:
    normalized = normalize_spoken_contract(value)
    return sha256(normalized.encode("utf-8")).hexdigest()


__all__ = ["normalize_spoken_contract", "spoken_contract_digest"]
