"""Human-readable rendering for deterministic evaluation reports."""

from __future__ import annotations

from .models import EvaluationReport


def render_markdown(report: EvaluationReport) -> str:
    summary = report.summary
    lines = [
        f"# Evaluation report: {report.profile}",
        "",
        f"- Run: `{report.run_id}`",
        f"- Harness: `{report.harness_version}`",
        f"- Scenario suite: `{report.scenario_suite_version}`",
        f"- Seed: `{report.seed}`",
        f"- Scenarios passed: **{summary['scenarios_passed']}/{summary['scenario_count']}**",
        f"- Critical failures: **{summary['critical_failures']}**",
        f"- Major failures: **{summary['major_failures']}**",
        f"- Negative controls detected: **{summary['negative_controls_detected']}/{summary['negative_control_count']}**",
        f"- Promotion eligible: **{'yes' if report.promotion_eligible else 'no'}**",
        "",
        "## Scenario gates",
        "",
    ]
    for scenario in report.scenarios:
        lines.append(
            f"### {'PASS' if scenario.passed else 'FAIL'} — {scenario.scenario_id}"
        )
        lines.append("")
        lines.append(f"{scenario.description} Source: `{scenario.source}`.")
        lines.append("")
        for gate in scenario.gates:
            marker = "PASS" if gate.passed else "FAIL"
            lines.append(
                f"- **{marker} [{gate.severity}] `{gate.gate_id}`:** {gate.reason}"
            )
        lines.append("")
    lines.extend(["## Evaluator negative controls", ""])
    for control in report.negative_controls:
        marker = "DETECTED" if control.detected else "MISSED"
        lines.append(
            f"- **{marker}** `{control.control_id}` → `{control.expected_gate}`: "
            f"{control.reason}"
        )
    if report.comparison:
        lines.extend(["", "## Comparison", ""])
        for key, value in report.comparison.items():
            lines.append(f"- `{key}`: `{value}`")
    lines.append("")
    return "\n".join(lines)


__all__ = ["render_markdown"]
