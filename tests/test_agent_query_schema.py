"""Schema-level tests for ``agent_query`` — the path-B proxy tool.

``agent_query`` is the voice LLM's escape hatch for anything that needs
the full backend MCP catalog. It must:

1. Be present in ``VOICE_FUNCTION_SCHEMAS`` so the LLM knows it can
   call it.
2. NOT be in ``BUILTIN_TOOL_HANDLERS`` — if it were, ``register_voice_tools``
   would short-circuit it to a local handler and the backend would never
   see the query.

These two invariants together guarantee the tool gets dispatched
through ``VoiceTurnDispatcher`` to ``/voice/turn``, where the backend
special-cases the name and runs the full orchestrator.
"""

from __future__ import annotations

from voice.tool_catalog import VOICE_FUNCTION_SCHEMAS
from voice.tools.builtin_tools import BUILTIN_TOOL_HANDLERS


def test_agent_query_is_registered_with_the_llm() -> None:
    names = {schema.name for schema in VOICE_FUNCTION_SCHEMAS}
    assert "agent_query" in names, (
        "agent_query must be in VOICE_FUNCTION_SCHEMAS so the voice LLM "
        "knows it can call it. Without this, the LLM can't reach the "
        "backend's orchestrator for connected-app questions."
    )


def test_agent_query_routes_through_backend_not_locally() -> None:
    """If agent_query were in BUILTIN_TOOL_HANDLERS, ``register_voice_tools``
    would short-circuit it to a local function and never POST to
    /voice/turn. That would defeat the whole purpose of the proxy."""
    assert "agent_query" not in BUILTIN_TOOL_HANDLERS, (
        "agent_query must NOT be in BUILTIN_TOOL_HANDLERS — it has to "
        "dispatch to orchet-backend so the orchestrator can answer with "
        "the full MCP tool catalog."
    )


def test_agent_query_schema_takes_query_string() -> None:
    schema = next(
        s for s in VOICE_FUNCTION_SCHEMAS if s.name == "agent_query"
    )
    assert "query" in schema.properties
    assert schema.required == ["query"]
