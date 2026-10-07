from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from evals.runner import build_report, main


class EvaluationRunnerTests(unittest.TestCase):
    def test_reinforced_profile_is_promotion_eligible(self) -> None:
        report = build_report(profile="reinforced")

        self.assertTrue(report.promotion_eligible)
        self.assertEqual(11, report.summary["scenarios_passed"])
        self.assertEqual(0, report.summary["critical_failures"])
        self.assertEqual(
            report.summary["negative_control_count"],
            report.summary["negative_controls_detected"],
        )

    def test_baseline_reproduces_stale_confirmation_failure(self) -> None:
        report = build_report(profile="baseline")

        self.assertFalse(report.promotion_eligible)
        target = next(
            scenario
            for scenario in report.scenarios
            if scenario.scenario_id == "changed_proposal_requires_reconfirmation"
        )
        self.assertFalse(target.passed)
        failures = {gate.gate_id for gate in target.gates if not gate.passed}
        self.assertIn("confirmation_matches_active_proposal", failures)
        self.assertIn("success_only_after_final_verification", failures)

    def test_before_after_repairs_target_without_regression(self) -> None:
        baseline = build_report(profile="baseline")
        after = build_report(profile="reinforced", compare=baseline.to_dict())

        self.assertTrue(after.promotion_eligible)
        self.assertEqual(
            ["changed_proposal_requires_reconfirmation"],
            after.comparison["repaired_scenarios"],
        )
        self.assertEqual([], after.comparison["prior_pass_regressions"])

    def test_same_seed_and_profile_produce_identical_report(self) -> None:
        first = build_report(profile="reinforced", seed=117).to_dict()
        second = build_report(profile="reinforced", seed=117).to_dict()

        self.assertEqual(first, second)

    def test_cli_writes_machine_and_human_reports_and_uses_exit_gate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_path = root / "baseline.json"
            reinforced_path = root / "reinforced.json"

            baseline_exit = main(
                ["--profile", "baseline", "--output", str(baseline_path)]
            )
            reinforced_exit = main(
                ["--profile", "reinforced", "--output", str(reinforced_path)]
            )

            self.assertEqual(2, baseline_exit)
            self.assertEqual(0, reinforced_exit)
            self.assertTrue(baseline_path.with_suffix(".md").exists())
            self.assertTrue(reinforced_path.with_suffix(".md").exists())
            payload = json.loads(reinforced_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["promotion_eligible"])
            self.assertIn("scenario_suite_version", payload)
            self.assertIn("seed", payload)


if __name__ == "__main__":
    unittest.main()
