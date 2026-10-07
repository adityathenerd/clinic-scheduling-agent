"""Deterministic search over curated clinic FAQ records.

This deliberately small knowledge boundary is preferable to an ungrounded RAG
demo: every answer is inspectable, source-labelled, effective-dated, and limited
to administrative information. It can later be replaced by a governed search
service without changing the model-visible tool contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Iterable


DEFAULT_FAQ_FIXTURE = Path(__file__).with_name("clinic_faqs.json")

_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "are",
        "at",
        "can",
        "clinic",
        "do",
        "for",
        "from",
        "how",
        "i",
        "is",
        "it",
        "me",
        "of",
        "on",
        "the",
        "their",
        "there",
        "they",
        "to",
        "what",
        "when",
        "where",
        "who",
        "will",
        "with",
    }
)


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if token not in _STOP_WORDS
    )


def _overlap_count(left: frozenset[str], right: frozenset[str]) -> int:
    """Count conservative prefix matches such as park/parking or insure/insurance."""

    return sum(
        1
        for candidate in left
        if any(
            candidate == target
            or (
                min(len(candidate), len(target)) >= 4
                and (
                    candidate.startswith(target)
                    or target.startswith(candidate)
                )
            )
            for target in right
        )
    )


@dataclass(frozen=True, slots=True)
class ClinicFAQ:
    faq_id: str
    category: str
    title: str
    answer: str
    keywords: tuple[str, ...]
    source_label: str
    effective_from: str
    review_after: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "ClinicFAQ":
        return cls(
            faq_id=str(value["faq_id"]),
            category=str(value["category"]),
            title=str(value["title"]),
            answer=str(value["answer"]),
            keywords=tuple(str(item) for item in value.get("keywords", [])),
            source_label=str(value["source_label"]),
            effective_from=str(value["effective_from"]),
            review_after=str(value["review_after"]),
        )


class ClinicFAQKnowledgeBase:
    """Read-only ranked lookup over clinic-approved public answers."""

    def __init__(self, entries: Iterable[ClinicFAQ]) -> None:
        self.entries = tuple(entries)
        ids = [entry.faq_id for entry in self.entries]
        if len(ids) != len(set(ids)):
            raise ValueError("FAQ identifiers must be unique")

    @classmethod
    def load(cls, path: str | Path = DEFAULT_FAQ_FIXTURE) -> "ClinicFAQKnowledgeBase":
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise ValueError("FAQ fixture must contain a JSON array")
        return cls(ClinicFAQ.from_mapping(value) for value in values)

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        limit: int = 3,
    ) -> tuple[ClinicFAQ, ...]:
        if not query.strip():
            raise ValueError("FAQ query is required")
        if len(query) > 500:
            raise ValueError("FAQ query is too long")
        if limit < 1 or limit > 5:
            raise ValueError("FAQ result limit must be between 1 and 5")
        query_tokens = _tokens(query)
        ranked: list[tuple[int, str, ClinicFAQ]] = []
        for entry in self.entries:
            if category and entry.category != category:
                continue
            title_tokens = _tokens(entry.title)
            keyword_tokens = _tokens(" ".join(entry.keywords))
            answer_tokens = _tokens(entry.answer)
            score = (
                5 * _overlap_count(query_tokens, keyword_tokens)
                + 3 * _overlap_count(query_tokens, title_tokens)
                + _overlap_count(query_tokens, answer_tokens)
            )
            if score:
                ranked.append((score, entry.faq_id, entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return tuple(item[2] for item in ranked[:limit])
