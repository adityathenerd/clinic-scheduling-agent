"""CLI and orchestration for the deterministic evaluation harness."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .assertions import evaluate_observation
from .models import (
    DEFAULT_SEED,
    HARNESS_VERSION,
    SCENARIO_SUITE_VERSION,
    EvaluationReport,
    NegativeControlResult,
    ScenarioResult,
)
from .report import render_markdown
from .scenarios import negative_control_observations, run_scenario_observations


SCHEMA_VERSION = "clinic-agent-eval-report/v1"
EVALUATED_AT = "2026-10-05T10:00:00+05:30"


def _run_id(profile: str, seed: int) -> str:
    material = (
        f"{HARNESS_VERSION}:{SCENARIO_SUITE_VERSION}:{profile}:{seed}"
    ).encode("utf-8")
    return "eval-" + sha256(material).hexdigest()[:16]


def _load_comparison(path: str | Path | None) -> Mapping[str, Any] | None:
    if path is None:
        return None
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("comparison report has an unsupported schema version")
    if data.get("scenario_suite_version") != SCENARIO_SUITE_VERSION:
        raise ValueError("comparison report uses a different scenario suite")
    return data


def build_report(
    *,
    profile: str = "reinforced",
    seed: int = DEFAULT_SEED,
    compare: Mapping[str, Any] | None = None,
) -> EvaluationReport:
    scenarios: list[ScenarioResult] = []
    for observation in run_scenario_observations(profile):
        gates = evaluate_observation(observation.facts)
        passed = bool(gates) and all(gate.passed for gate in gates)
        scenarios.append(
            ScenarioResult(
                scenario_id=observation.scenario_id,
                description=observation.description,
                source=observation.source,
                passed=passed,
                gates=gates,
                events=observation.events,
                metrics=observation.metrics,
            )
        )

    negative_controls: list[NegativeControlResult] = []
    for control_id, (expected_gate, observation) in (
        negative_control_observations().items()
    ):
        gates = evaluate_observation(observation)
        failures = tuple(gate.gate_id for gate in gates if not gate.passed)
        detected = expected_gate in failures
        negative_controls.append(
            NegativeControlResult(
                control_id=control_id,
                expected_gate=expected_gate,
                detected=detected,
                detected_failures=failures,
                reason=(
                    "Known-bad evidence was rejected by the expected gate."
                    if detected
                    else "Known-bad evidence escaped the expected gate."
                ),
            )
        )

    failed_gates = [
        gate
        for scenario in scenarios
        for gate in scenario.gates
        if not gate.passed
    ]
    critical_failures = sum(gate.severity == "critical" for gate in failed_gates)
    major_failures = sum(gate.severity == "major" for gate in failed_gates)
    passed_count = sum(scenario.passed for scenario in scenarios)
    controls_detected = sum(control.detected for control in negative_controls)
    promotion_eligible = bool(
        passed_count == len(scenarios)
        and critical_failures == 0
        and major_failures == 0
        and controls_detected == len(negative_controls)
    )

    comparison: Mapping[str, Any] | None = None
    if compare is not None:
        previous = {
            item["scenario_id"]: bool(item["passed"])
            for item in compare.get("scenarios", ())
        }
        current = {item.scenario_id: item.passed for item in scenarios}
        regressions = sorted(
            scenario_id
            for scenario_id, was_passing in previous.items()
            if was_passing and not current.get(scenario_id, False)
        )
        repaired = sorted(
            scenario_id
            for scenario_id, was_passing in previous.items()
            if not was_passing and current.get(scenario_id, False)
        )
        comparison = {
            "previous_run_id": compare.get("run_id"),
            "previous_profile": compare.get("profile"),
            "previous_scenarios_passed": compare.get("summary", {}).get(
                "scenarios_passed", 0
            ),
            "current_scenarios_passed": passed_count,
            "repaired_scenarios": repaired,
            "prior_pass_regressions": regressions,
            "promotion_improved": bool(repaired and not regressions),
        }
        if regressions:
            promotion_eligible = False

    summary = {
        "scenario_count": len(scenarios),
        "scenarios_passed": passed_count,
        "scenarios_failed": len(scenarios) - passed_count,
        "critical_failures": critical_failures,
        "major_failures": major_failures,
        "negative_control_count": len(negative_controls),
        "negative_controls_detected": controls_detected,
    }
    return EvaluationReport(
        schema_version=SCHEMA_VERSION,
        harness_version=HARNESS_VERSION,
        scenario_suite_version=SCENARIO_SUITE_VERSION,
        seed=seed,
        run_id=_run_id(profile, seed),
        profile=profile,
        evaluated_at=EVALUATED_AT,
        promotion_eligible=promotion_eligible,
        summary=summary,
        scenarios=tuple(scenarios),
        negative_controls=tuple(negative_controls),
        comparison=comparison,
    )


def _write_report(report: EvaluationReport, output: Path) -> tuple[Path, Path]:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown = output.with_suffix(".md")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    return output, markdown


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run credential-free scheduling safety evaluations."
    )
    parser.add_argument(
        "--profile",
        choices=("baseline", "reinforced"),
        default="reinforced",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        help="JSON report path (default: reports/eval-<profile>.json).",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        help="Optional earlier JSON report from the same scenario suite.",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Print the complete machine-readable report.",
    )
    args = parser.parse_args(argv)
    output = args.output or Path("reports") / f"eval-{args.profile}.json"
    report = build_report(
        profile=args.profile,
        seed=args.seed,
        compare=_load_comparison(args.compare),
    )
    json_path, markdown_path = _write_report(report, output)
    if args.print_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(
            "Evaluation "
            f"profile={report.profile} "
            f"passed={report.summary['scenarios_passed']}/"
            f"{report.summary['scenario_count']} "
            f"critical_failures={report.summary['critical_failures']} "
            f"major_failures={report.summary['major_failures']} "
            f"negative_controls={report.summary['negative_controls_detected']}/"
            f"{report.summary['negative_control_count']} "
            f"promotion_eligible={str(report.promotion_eligible).lower()}"
        )
        print(f"JSON report: {json_path}")
        print(f"Markdown report: {markdown_path}")
    return 0 if report.promotion_eligible else 2


if __name__ == "__main__":
    raise SystemExit(main())
