from __future__ import annotations

import unittest

from clinic_agent.agent.live_config import (
    build_backend_tool_update,
    build_live_session_config,
)
from clinic_agent.agent.prompts import PromptContext
from clinic_agent.agent.tool_catalog import schemas_for


def context() -> PromptContext:
    return PromptContext(
        assistant_name="Test Assistant",
        clinic_name="Synthetic Clinic",
        approved_urgent_message="Please hold while I connect you to the clinic team.",
        constitution_version="0.2",
        constitution_hash="abc123",
        clinic_policy_version="policy-v0.1",
    )


class LiveConfigTests(unittest.TestCase):
    def test_session_registers_tools_on_delegated_backend_only(self) -> None:
        config = build_live_session_config(
            context(), allowed_tool_names=("search_slots", "create_appointment")
        )
        self.assertNotIn("tools", config)
        responses = config["delegation"]["responses"]
        self.assertEqual(responses["model"], "gpt-6-sol")
        self.assertFalse(responses["parallel_tool_calls"])
        self.assertEqual(
            [tool["name"] for tool in responses["tools"]],
            ["search_slots", "create_appointment"],
        )

    def test_initial_session_can_expose_no_scheduler_tools(self) -> None:
        config = build_live_session_config(context())
        self.assertEqual(config["delegation"]["responses"]["tools"], [])

    def test_tool_update_changes_only_backend_capabilities(self) -> None:
        update = build_backend_tool_update(("list_appointments", "delete_appointment"))
        responses = update["delegation"]["responses"]
        self.assertEqual(
            [tool["name"] for tool in responses["tools"]],
            ["list_appointments", "delete_appointment"],
        )
        self.assertFalse(responses["parallel_tool_calls"])

    def test_unknown_tool_is_rejected_before_session_update(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown tool names"):
            schemas_for(("invented_tool",))

    def test_identity_interpretation_uses_strict_semantic_enum(self) -> None:
        (tool,) = schemas_for(("interpret_identity_response",))

        self.assertTrue(tool["strict"])
        self.assertEqual(
            ["affirmed", "denied", "unclear"],
            tool["parameters"]["properties"]["decision"]["enum"],
        )
        self.assertFalse(tool["parameters"]["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
