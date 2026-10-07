"""Model-visible tool catalog for GPT-Live Responses delegation.

Only domain arguments are visible to the model. The Python control plane adds
identity, authorization, versions, proposal references, confirmation tokens,
correction epochs, and idempotency keys before any protected operation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping

from clinic_agent.control_plane.tool_contracts import (
    DELETE_APPOINTMENT_TOOL,
    EDIT_APPOINTMENT_TOOL,
)


IDENTITY_INTERPRETATION_TOOL_NAME = "interpret_identity_response"
MUTATION_CONFIRMATION_TOOL_NAME = "interpret_mutation_confirmation"
FOLLOW_UP_INTERPRETATION_TOOL_NAME = "interpret_follow_up_decision"
FAQ_SEARCH_TOOL_NAME = "search_clinic_faqs"


def _function_tool(
    name: str,
    description: str,
    properties: Mapping[str, Any],
    required: tuple[str, ...],
    *,
    strict: bool = False,
) -> Mapping[str, Any]:
    tool: dict[str, Any] = {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": dict(properties),
            "required": list(required),
            "additionalProperties": False,
        },
    }
    if strict:
        tool["strict"] = True
    return tool


INTERPRET_IDENTITY_RESPONSE_TOOL = _function_tool(
    IDENTITY_INTERPRETATION_TOOL_NAME,
    (
        "Semantically classify only the caller's latest answer to the active "
        "application identity question. This records evidence; it does not grant "
        "authorization. Use affirmed only when the caller clearly says they are "
        "the expected patient, denied when they clearly say they are not, and "
        "unclear otherwise."
    ),
    {
        "decision": {
            "type": "string",
            "enum": ["affirmed", "denied", "unclear"],
            "description": "Semantic meaning of the latest caller answer.",
        },
    },
    ("decision",),
    strict=True,
)

INTERPRET_MUTATION_CONFIRMATION_TOOL = _function_tool(
    MUTATION_CONFIRMATION_TOOL_NAME,
    (
        "Classify only the caller's latest completed answer to one exact pending "
        "appointment proposal. Confirmed means unambiguous permission to execute "
        "that proposal unchanged. Any requested change is correction."
    ),
    {
        "decision": {
            "type": "string",
            "enum": ["confirmed", "denied", "correction", "unclear"],
            "description": "Semantic meaning of the answer for the exact proposal.",
        },
    },
    ("decision",),
    strict=True,
)

INTERPRET_FOLLOW_UP_DECISION_TOOL = _function_tool(
    FOLLOW_UP_INTERPRETATION_TOOL_NAME,
    (
        "Classify only the caller's continuation intent in the supplied context. "
        "accepted means yes to an explicit offer without a stated task; declined "
        "means no or clearly closing the call; new_request means the caller clearly "
        "states another scheduling task; unclear means none of those."
    ),
    {
        "decision": {
            "type": "string",
            "enum": ["accepted", "declined", "new_request", "unclear"],
        },
    },
    ("decision",),
    strict=True,
)

SEARCH_CLINIC_FAQS_TOOL = _function_tool(
    FAQ_SEARCH_TOOL_NAME,
    (
        "Search clinic-approved public FAQs about insurance, services, providers, "
        "arrival, parking, clinic logistics, prescriptions, and financial support. "
        "May be used before identity verification. Never use it for diagnosis or "
        "personalized treatment or medication advice."
    ),
    {
        "query": {
            "type": "string",
            "description": "The caller's clinic-information question in plain language.",
        },
        "category": {
            "type": "string",
            "enum": [
                "insurance",
                "services",
                "providers",
                "visit",
                "location",
                "medicines",
            ],
            "description": "Optional FAQ category when confidently known.",
        },
    },
    ("query",),
)


LIST_APPOINTMENTS_TOOL = _function_tool(
    "list_appointments",
    "List the verified patient's appointments when application state permits disclosure.",
    {},
    (),
)

GET_APPOINTMENT_TOOL = _function_tool(
    "get_appointment",
    "Read one selected appointment from authoritative state.",
    {"appointment_id": {"type": "string"}},
    ("appointment_id",),
)

CHECK_ELIGIBILITY_TOOL = _function_tool(
    "check_eligibility",
    "Check administrative eligibility and prerequisites for an appointment type.",
    {"appointment_type": {"type": "string"}},
    ("appointment_type",),
)

SEARCH_SLOTS_TOOL = _function_tool(
    "search_slots",
    (
        "Search fresh appointment availability using canonical clinic identifiers. "
        "Use location_id from an appointment or prior tool result; never put a "
        "patient-facing label such as '2care Clinic, Koramangala, Bengaluru' "
        "in this field."
    ),
    {
        "appointment_type": {"type": "string"},
        "date_from": {"type": "string", "description": "ISO 8601 date"},
        "date_to": {"type": "string", "description": "ISO 8601 date"},
        "time_window": {"type": "string"},
        "location_id": {
            "type": "string",
            "description": "Canonical clinic location ID, for example downtown.",
        },
        "provider_id": {"type": "string"},
    },
    ("appointment_type", "date_from", "date_to"),
)

CREATE_APPOINTMENT_TOOL = _function_tool(
    "create_appointment",
    (
        "Request creation of the exact active confirmed appointment proposal. "
        "Application guards decide whether it is authorized."
    ),
    {"slot_id": {"type": "string"}},
    ("slot_id",),
)


BACKEND_TOOL_CATALOG: Mapping[str, Mapping[str, Any]] = {
    tool["name"]: tool
    for tool in (
        INTERPRET_IDENTITY_RESPONSE_TOOL,
        SEARCH_CLINIC_FAQS_TOOL,
        LIST_APPOINTMENTS_TOOL,
        GET_APPOINTMENT_TOOL,
        CHECK_ELIGIBILITY_TOOL,
        SEARCH_SLOTS_TOOL,
        CREATE_APPOINTMENT_TOOL,
        EDIT_APPOINTMENT_TOOL,
        DELETE_APPOINTMENT_TOOL,
    )
}


def schemas_for(allowed_tool_names: Iterable[str]) -> tuple[Mapping[str, Any], ...]:
    """Return a deterministic least-privilege subset for session registration."""

    names = tuple(dict.fromkeys(allowed_tool_names))
    unknown = set(names).difference(BACKEND_TOOL_CATALOG)
    if unknown:
        raise ValueError(f"unknown tool names: {sorted(unknown)}")
    return tuple(BACKEND_TOOL_CATALOG[name] for name in names)
