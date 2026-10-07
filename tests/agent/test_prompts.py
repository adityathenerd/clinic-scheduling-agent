from __future__ import annotations

import unittest

from clinic_agent.agent.prompts import (
    PromptContext,
    build_backend_prompt,
    build_live_prompt,
    build_prompt_manifest,
)


def context() -> PromptContext:
    return PromptContext(
        assistant_name="Test Assistant",
        clinic_name="Synthetic Clinic",
        approved_urgent_message="Please hold while I connect you to the clinic team.",
        constitution_version="0.2",
        constitution_hash="abc123",
        clinic_policy_version="policy-v0.1",
    )


class PromptTests(unittest.TestCase):
    def test_live_prompt_contains_constitution_and_delegation_boundaries(self) -> None:
        prompt = build_live_prompt(context())
        self.assertIn("Agent Constitution 0.2", prompt)
        self.assertIn("Delegate before answering", prompt)
        self.assertIn("never imply success", prompt)
        self.assertIn("Test Assistant", prompt)
        self.assertIn("Application-owned gate turns", prompt)
        self.assertIn("remain silent until", prompt)
        self.assertIn("without a preamble, postscript", prompt)

    def test_backend_prompt_keeps_authority_in_python(self) -> None:
        prompt = build_backend_prompt(context())
        self.assertIn("The Python application owns them", prompt)
        self.assertIn("Never issue mutations in parallel", prompt)
        self.assertIn("soft cancellation", prompt)
        self.assertIn("one-time confirmation token", prompt)
        self.assertIn("Do not ask a preliminary yes/no confirmation", prompt)
        self.assertIn("speak that instruction once", prompt)

    def test_clinic_address_and_ist_are_explicit_patient_facing_constraints(self) -> None:
        subject = PromptContext(
            assistant_name="Mira",
            clinic_name="2care Clinic",
            clinic_address="Koramangala, Bengaluru",
            clinic_timezone_label="IST",
            approved_urgent_message="Contact emergency services.",
            constitution_version="0.2",
            constitution_hash="abc123",
            clinic_policy_version="2care-clinic-v0.2",
        )

        for prompt in (build_live_prompt(subject), build_backend_prompt(subject)):
            self.assertIn("Koramangala, Bengaluru", prompt)
            self.assertIn("appointment dates and times use IST", prompt)
            self.assertIn("never read a UTC offset aloud", prompt)

    def test_context_rejects_missing_policy_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved_urgent_message"):
            PromptContext(
                assistant_name="Test Assistant",
                clinic_name="Synthetic Clinic",
                approved_urgent_message="",
                constitution_version="0.2",
                constitution_hash="abc123",
                clinic_policy_version="policy-v0.1",
            )

    def test_manifest_hashes_rendered_prompts(self) -> None:
        manifest = build_prompt_manifest(context())
        self.assertEqual(manifest.constitution_version, "0.2")
        self.assertEqual(len(manifest.live_prompt_hash), 64)
        self.assertEqual(len(manifest.backend_prompt_hash), 64)
        self.assertNotEqual(manifest.live_prompt_hash, manifest.backend_prompt_hash)


if __name__ == "__main__":
    unittest.main()
