from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO

from appointment_harness.demo import main, run_scenario
from appointment_harness.models import AppointmentStatus
from clinic_agent.control_plane.tool_contracts import FollowUpOutcome


class DemoTests(unittest.TestCase):
    def test_cancel_scenario_includes_verified_follow_up(self) -> None:
        harness, result = run_scenario("cancel")
        self.assertIs(result["appointment"].status, AppointmentStatus.CANCELLED)
        self.assertIs(result["follow_up"].outcome, FollowUpOutcome.ACCEPTED)
        self.assertEqual(len(harness.store.follow_ups), 1)

    def test_follow_up_outage_keeps_cancelled_state(self) -> None:
        harness, result = run_scenario("follow-up-outage")
        self.assertIs(result["appointment"].status, AppointmentStatus.CANCELLED)
        self.assertIs(result["follow_up"].outcome, FollowUpOutcome.FAILED)
        self.assertIs(
            harness.store.appointments["appointment-existing"].status,
            AppointmentStatus.CANCELLED,
        )

    def test_json_frontend_is_machine_readable(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["--scenario", "reschedule", "--json"])
        parsed = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(parsed["scenario"], "reschedule")
        self.assertEqual(parsed["result"]["appointment"]["slot_id"], "slot-1630")
        self.assertTrue(parsed["audit"])


if __name__ == "__main__":
    unittest.main()
