from __future__ import annotations

from types import SimpleNamespace
import unittest

from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    FollowUpDecision,
    IdentityDecision,
)
from clinic_agent.runtime.identity_classifier import ResponsesIdentityTurnClassifier


class FakeResponses:
    def __init__(self, output: list[object]) -> None:
        self.output = output
        self.requests: list[dict[str, object]] = []

    async def create(self, **request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(output=self.output)


class ResponsesIdentityTurnClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_forces_one_strict_semantic_function_call_without_storage(self) -> None:
        responses = FakeResponses(
            [
                SimpleNamespace(
                    type="function_call",
                    name="interpret_identity_response",
                    arguments='{"decision":"affirmed"}',
                )
            ]
        )
        client = SimpleNamespace(responses=responses)
        subject = ResponsesIdentityTurnClassifier(client)

        decision = await subject.classify(
            transcript="Yes, speaking.",
            expected_patient_name="Asha Rao",
        )

        self.assertEqual(IdentityDecision.AFFIRMED, decision)
        request = responses.requests[0]
        self.assertEqual(
            {"type": "function", "name": "interpret_identity_response"},
            request["tool_choice"],
        )
        self.assertFalse(request["parallel_tool_calls"])
        self.assertFalse(request["store"])
        self.assertTrue(request["tools"][0]["strict"])  # type: ignore[index]

    async def test_rejects_missing_or_multiple_semantic_decisions(self) -> None:
        client = SimpleNamespace(responses=FakeResponses([]))
        subject = ResponsesIdentityTurnClassifier(client)

        with self.assertRaisesRegex(RuntimeError, "exactly one decision"):
            await subject.classify(
                transcript="Maybe.",
                expected_patient_name="Asha Rao",
            )

    async def test_forces_proposal_bound_confirmation_decision(self) -> None:
        responses = FakeResponses(
            [
                SimpleNamespace(
                    type="function_call",
                    name="interpret_mutation_confirmation",
                    arguments='{"decision":"confirmed"}',
                )
            ]
        )
        subject = ResponsesIdentityTurnClassifier(
            SimpleNamespace(responses=responses)
        )

        decision = await subject.classify_confirmation(
            transcript="And yes, I am confirming that proposal.",
            exact_proposal="Move the visit from October 8 to October 12 at 11:30 AM.",
        )

        self.assertEqual(ConfirmationDecision.CONFIRMED, decision)
        self.assertEqual(
            {
                "type": "function",
                "name": "interpret_mutation_confirmation",
            },
            responses.requests[0]["tool_choice"],
        )

    async def test_forces_semantic_follow_up_decision(self) -> None:
        responses = FakeResponses(
            [
                SimpleNamespace(
                    type="function_call",
                    name="interpret_follow_up_decision",
                    arguments='{"decision":"new_request"}',
                )
            ]
        )
        subject = ResponsesIdentityTurnClassifier(SimpleNamespace(responses=responses))

        decision = await subject.classify_follow_up(
            transcript="Please book a new appointment next Friday",
            offered_help="Would you like help with another appointment?",
        )

        self.assertEqual(FollowUpDecision.NEW_REQUEST, decision)
        request = responses.requests[0]
        self.assertEqual(
            {"type": "function", "name": "interpret_follow_up_decision"},
            request["tool_choice"],
        )
        self.assertTrue(request["tools"][0]["strict"])  # type: ignore[index]
        instructions = str(request["instructions"])
        self.assertIn("later request controls", instructions)
        self.assertIn("public clinic-information question", instructions)


if __name__ == "__main__":
    unittest.main()
