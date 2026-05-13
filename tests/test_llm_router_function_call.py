from __future__ import annotations

from typing import Any

from voice.pipeline import VoiceMetadata
from voice.tool_catalog import VOICE_FUNCTION_SCHEMAS
from voice.transport import register_voice_tools
from voice.voice_turn_dispatcher import VoiceTurnDispatchOutcome


class FakeLLM:
    def __init__(self) -> None:
        self.registered: list[tuple[str | None, bool]] = []

    def create_context_aggregator(self, context: Any, **kwargs: Any) -> object:
        del context, kwargs
        return object()

    def register_function(
        self,
        function_name: str | None,
        callback: Any,
        start_callback: Any | None = None,
        *,
        cancel_on_interruption: bool = False,
    ) -> None:
        del callback, start_callback
        self.registered.append((function_name, cancel_on_interruption))


class FakeDispatcher:
    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        transport: object,
    ) -> VoiceTurnDispatchOutcome:
        del tool_name, arguments, transport
        return VoiceTurnDispatchOutcome(
            function_result={"ok": True},
            spoken_text=None,
            run_llm=True,
        )


def test_register_voice_tools_registers_every_function_with_interruption_cancel(
    settings: Any,
) -> None:
    llm = FakeLLM()
    metadata = VoiceMetadata(
        voice_session_id="voice_test",
        user_id="user_test",
        client_kind="web",
    )

    register_voice_tools(
        llm,
        FakeDispatcher(),
        object(),
        settings=settings,
        metadata=metadata,
    )

    assert [name for name, _ in llm.registered] == [
        schema.name for schema in VOICE_FUNCTION_SCHEMAS
    ]
    assert all(cancel_on_interruption for _, cancel_on_interruption in llm.registered)
