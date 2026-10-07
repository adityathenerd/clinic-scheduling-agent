"""Stable report models for deterministic evaluation runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


HARNESS_VERSION = "1.0.0"
SCENARIO_SUITE_VERSION = "2026-10-07.2"
DEFAULT_SEED = 42


@dataclass(frozen=True, slots=True)
class GateResult:
    gate_id: str
    severity: str
    passed: bool
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    scenario_id: str
    description: str
    source: str
    passed: bool
    gates: tuple[GateResult, ...]
    events: tuple[Mapping[str, Any], ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NegativeControlResult:
    control_id: str
    expected_gate: str
    detected: bool
    detected_failures: tuple[str, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    schema_version: str
    harness_version: str
    scenario_suite_version: str
    seed: int
    run_id: str
    profile: str
    evaluated_at: str
    promotion_eligible: bool
    summary: Mapping[str, Any]
    scenarios: tuple[ScenarioResult, ...]
    negative_controls: tuple[NegativeControlResult, ...]
    comparison: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
