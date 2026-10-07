"""Deterministic fault schedule for boundary and recovery tests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from threading import Lock


class FaultPoint(str, Enum):
    BEFORE_READ = "before_read"
    AFTER_READ = "after_read"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"
    BEFORE_FINAL_READ = "before_final_read"
    FOLLOW_UP = "follow_up"


class FaultKind(str, Enum):
    TIMEOUT = "timeout"
    FAILURE = "failure"
    SLOT_RACE = "slot_race"
    STALE_RESPONSE = "stale_response"
    FINAL_READ_CONFLICT = "final_read_conflict"
    FOLLOW_UP_OUTAGE = "follow_up_outage"


@dataclass(frozen=True, slots=True)
class Fault:
    operation: str
    point: FaultPoint
    kind: FaultKind
    occurrence: int = 1

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise ValueError("fault operation is required")
        if self.occurrence < 1:
            raise ValueError("fault occurrence must be at least one")


class SyntheticHarnessError(RuntimeError):
    """Base class for expected injected or contract failures."""


class SyntheticTimeout(SyntheticHarnessError):
    pass


class SyntheticFailure(SyntheticHarnessError):
    pass


class ContractRejected(SyntheticHarnessError):
    pass


class VersionConflict(SyntheticHarnessError):
    pass


class IdempotencyConflict(SyntheticHarnessError):
    pass


class FaultInjector:
    def __init__(self, faults: tuple[Fault, ...] = ()) -> None:
        self._faults = faults
        self._seen: dict[tuple[str, FaultPoint, FaultKind], int] = {}
        self._lock = Lock()

    def trigger(self, operation: str, point: FaultPoint) -> FaultKind | None:
        with self._lock:
            for fault in self._faults:
                if fault.operation != operation or fault.point is not point:
                    continue
                key = (fault.operation, fault.point, fault.kind)
                seen = self._seen.get(key, 0) + 1
                self._seen[key] = seen
                if seen == fault.occurrence:
                    return fault.kind
        return None
