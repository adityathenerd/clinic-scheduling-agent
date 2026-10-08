"""Semantic identity-answer classification owned by the application.

The conversational frontend may fail to delegate a completed turn.  Identity
verification is too important to depend on that provider behavior, so the
application classifies the completed transcript with a forced, strict
Responses function call and applies the resulting immutable decision itself.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from clinic_agent.agent.tool_catalog import (
    INTERPRET_IDENTITY_RESPONSE_TOOL,
    INTERPRET_FOLLOW_UP_DECISION_TOOL,
    INTERPRET_MUTATION_CONFIRMATION_TOOL,
)
from clinic_agent.control_plane.state_machine import (
    ConfirmationDecision,
    FollowUpDecision,
    IdentityDecision,
)


class IdentityTurnClassifier(Protocol):
    async def classify(
        self, *, transcript: str, expected_patient_name: str
    ) -> IdentityDecision: ...


class ConfirmationTurnClassifier(Protocol):
    async def classify_confirmation(
        self, *, transcript: str, exact_proposal: str
    ) -> ConfirmationDecision: ...


class FollowUpTurnClassifier(Protocol):
    async def classify_follow_up(
        self, *, transcript: str, offered_help: str
    ) -> FollowUpDecision: ...


class ResponsesIdentityTurnClassifier:
    """Classify one completed answer without granting authorization."""

    def __init__(self, client: Any, *, model: str = "gpt-6-sol") -> None:
        self.client = client
        self.model = model

    async def classify(
        self, *, transcript: str, expected_patient_name: str
    ) -> IdentityDecision:
        if not transcript.strip():
            raise ValueError("identity transcript is required")
        response = await self.client.responses.create(
            model=self.model,
            instructions=(
                "Classify only whether the caller's answer affirms that they are "
                "the expected patient. Do not infer identity from a phone number, "
                "context, or politeness. Use unclear for questions, silence, or "
                "anything that is not a clear affirmation or denial. You must call "
                "interpret_identity_response exactly once."
            ),
            input=(
                f"Expected patient: {expected_patient_name}\n"
                f"Caller's completed answer: {transcript}"
            ),
            tools=[INTERPRET_IDENTITY_RESPONSE_TOOL],
            tool_choice={
                "type": "function",
                "name": "interpret_identity_response",
            },
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
            store=False,
        )
        calls = [
            item
            for item in response.output
            if getattr(item, "type", None) == "function_call"
            and getattr(item, "name", None) == "interpret_identity_response"
        ]
        if len(calls) != 1:
            raise RuntimeError("identity classifier did not return exactly one decision")
        arguments = json.loads(calls[0].arguments)
        try:
            return IdentityDecision(str(arguments["decision"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeError("identity classifier returned an invalid decision") from exc

    async def classify_confirmation(
        self, *, transcript: str, exact_proposal: str
    ) -> ConfirmationDecision:
        if not transcript.strip() or not exact_proposal.strip():
            raise ValueError("confirmation transcript and exact proposal are required")
        response = await self.client.responses.create(
            model=self.model,
            instructions=(
                "Classify only the caller's completed answer to the exact proposal. "
                "Use confirmed only for unambiguous permission to execute it exactly "
                "as stated. Use denied for an explicit refusal, correction if any "
                "detail is changed or qualified, and unclear otherwise. You must call "
                "interpret_mutation_confirmation exactly once."
            ),
            input=(
                f"Exact pending proposal: {exact_proposal}\n"
                f"Caller's completed answer: {transcript}"
            ),
            tools=[INTERPRET_MUTATION_CONFIRMATION_TOOL],
            tool_choice={
                "type": "function",
                "name": "interpret_mutation_confirmation",
            },
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
            store=False,
        )
        calls = [
            item
            for item in response.output
            if getattr(item, "type", None) == "function_call"
            and getattr(item, "name", None) == "interpret_mutation_confirmation"
        ]
        if len(calls) != 1:
            raise RuntimeError(
                "confirmation classifier did not return exactly one decision"
            )
        arguments = json.loads(calls[0].arguments)
        try:
            return ConfirmationDecision(str(arguments["decision"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeError(
                "confirmation classifier returned an invalid decision"
            ) from exc

    async def classify_follow_up(
        self, *, transcript: str, offered_help: str
    ) -> FollowUpDecision:
        if not transcript.strip() or not offered_help.strip():
            raise ValueError("follow-up transcript and offered help are required")
        response = await self.client.responses.create(
            model=self.model,
            instructions=(
                "Classify only the caller's continuation intent in the supplied "
                "conversation context. Use new_request when the caller clearly states "
                "another scheduling task, asks for appointment information, or asks a "
                "public clinic-information question such as parking, insurance, arrival, "
                "or provider information. If a turn starts with no, that's all, goodbye, "
                "or similar closing language but then contains a substantive request, "
                "the later request controls and the decision must be new_request. Use "
                "accepted for a plain yes to an explicit offer, declined only when the "
                "caller closes with no further request, and unclear otherwise. Never "
                "infer a scheduling change from politeness. "
                "You must call interpret_follow_up_decision exactly once."
            ),
            input=(
                f"Conversation context: {offered_help}\n"
                f"Caller's completed answer: {transcript}"
            ),
            tools=[INTERPRET_FOLLOW_UP_DECISION_TOOL],
            tool_choice={"type": "function", "name": "interpret_follow_up_decision"},
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
            store=False,
        )
        calls = [
            item for item in response.output
            if getattr(item, "type", None) == "function_call"
            and getattr(item, "name", None) == "interpret_follow_up_decision"
        ]
        if len(calls) != 1:
            raise RuntimeError("follow-up classifier did not return exactly one decision")
        arguments = json.loads(calls[0].arguments)
        try:
            return FollowUpDecision(str(arguments["decision"]))
        except (KeyError, ValueError) as exc:
            raise RuntimeError("follow-up classifier returned an invalid decision") from exc


__all__ = [
    "ConfirmationTurnClassifier",
    "FollowUpTurnClassifier",
    "IdentityTurnClassifier",
    "ResponsesIdentityTurnClassifier",
]
