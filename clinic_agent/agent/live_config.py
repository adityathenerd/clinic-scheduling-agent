"""GPT-Live session configuration and backend tool exposure updates."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Mapping

from .prompts import PromptContext, build_backend_prompt, build_live_prompt
from .tool_catalog import schemas_for


def build_live_session_config(
    context: PromptContext,
    *,
    allowed_tool_names: Iterable[str] = (),
) -> Mapping[str, Any]:
    """Create the direct Live API config used by the provider adapter."""

    return {
        "model": "gpt-live-1",
        "instructions": build_live_prompt(context),
        "delegation": {
            "type": "responses",
            "responses": {
                "model": "gpt-6-sol",
                "reasoning": {"effort": "low"},
                "instructions": build_backend_prompt(context),
                "tools": list(schemas_for(allowed_tool_names)),
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            },
        },
    }


def build_backend_tool_update(
    allowed_tool_names: Iterable[str],
) -> Mapping[str, Any]:
    """Build the session.update fragment for a workflow-state tool change."""

    return {
        "delegation": {
            "responses": {
                "tools": list(schemas_for(allowed_tool_names)),
                "tool_choice": "auto",
                "parallel_tool_calls": False,
            }
        }
    }

