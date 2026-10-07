from __future__ import annotations

import unittest

from appointment_harness.fixtures import load_fixture
from appointment_harness.service import AppointmentHarness
from clinic_agent.control_plane.state_machine import ConversationStateMachine
from clinic_agent.knowledge import ClinicFAQKnowledgeBase
from clinic_agent.runtime.text_session import GuardedToolRuntime


class ClinicFAQKnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subject = ClinicFAQKnowledgeBase.load()

    def test_insurance_question_returns_sourced_policy(self) -> None:
        result = self.subject.search("Do you accept insurance and is it cashless?")

        self.assertEqual("insurance-verification", result[0].faq_id)
        self.assertEqual("demo-clinic-billing-policy-v1", result[0].source_label)
        self.assertIn("must not promise", result[0].answer)

    def test_new_doctor_question_returns_joining_date(self) -> None:
        result = self.subject.search("Which new doctor is coming and from when?")

        self.assertEqual("provider-iyer-joining", result[0].faq_id)
        self.assertIn("October 12, 2026", result[0].answer)

    def test_location_answer_uses_2care_koramangala_and_ist(self) -> None:
        location = self.subject.search(
            "What is the clinic address and when are you open?",
            category="location",
        )

        combined = " ".join(item.answer for item in location)
        self.assertIn("2care Clinic", combined)
        self.assertIn("Koramangala, Bengaluru", combined)
        self.assertIn("IST", combined)
        self.assertNotIn("Downtown clinic", combined)

    def test_medicine_concession_answer_does_not_recommend_treatment_change(self) -> None:
        result = self.subject.search(
            "Can I get a concession on my prescribed medicine?",
            category="medicines",
        )

        self.assertEqual("medicine-concessions", result[0].faq_id)
        self.assertIn("does not guarantee", result[0].answer)
        self.assertIn("must not recommend changing", result[0].answer)

    def test_public_faq_is_available_before_identity_but_appointments_are_not(self) -> None:
        store, clock = load_fixture()
        harness = AppointmentHarness(store, clock)
        state = ConversationStateMachine(patient_display_name="Asha Rao")
        state.begin_outbound_call(
            assistant_name="Mira", clinic_name="Aster Demo Clinic"
        )
        runtime = GuardedToolRuntime(
            harness,
            session_id="faq-before-identity",
            patient_id="patient-001",
            state_machine=state,
        )

        faq = runtime.execute(
            "search_clinic_faqs", {"query": "Where can I park?", "category": "location"}
        )
        appointments = runtime.execute("list_appointments", {})

        self.assertEqual("ok", faq["status"])
        self.assertEqual("parking-and-transport", faq["matches"][0]["faq_id"])
        self.assertEqual("rejected", appointments["status"])
        self.assertEqual({}, store.authorized_sessions)


if __name__ == "__main__":
    unittest.main()
