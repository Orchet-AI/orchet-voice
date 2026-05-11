from __future__ import annotations

import json

import httpx

from voice.pipeline import VoiceMetadata, VoiceTurnTracker
from voice.voice_turn_dispatcher import VoiceTurnDispatcher


async def test_voice_turn_dispatcher_executed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "outcome": "executed",
                "tool_call_id": "tc_test",
                "result": {"messages": ["Tokyo mail"]},
                "voice_message_hint": "I found one matching email.",
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.orchet.ai",
        transport=httpx.MockTransport(handler),
    )
    metadata = VoiceMetadata(voice_session_id="voice_test", user_id="user_test")
    tracker = VoiceTurnTracker(metadata)
    turn = tracker.start_turn("turn_executed")
    turn.user_transcript = "find my Tokyo email"
    dispatcher = VoiceTurnDispatcher(
        gateway_url="https://api.orchet.ai",
        internal_token="test-internal",
        metadata=metadata,
        tracker=tracker,
        http_client=client,
    )

    outcome = await dispatcher.dispatch(
        "gmail_search_messages",
        {"query": "Tokyo"},
        transport=FakeTransport(),
    )

    assert outcome.function_result == {
        "outcome": "executed",
        "result": {"messages": ["Tokyo mail"]},
    }
    assert outcome.spoken_text == "I found one matching email."
    assert outcome.run_llm is False
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer test-internal"
    assert requests[0].headers["idempotency-key"]
    payload = json.loads(requests[0].content)
    assert payload["session_id"] == "voice_test"
    assert payload["turn_id"] == "turn_executed"
    assert payload["tool_call"] == {
        "name": "gmail_search_messages",
        "arguments": {"query": "Tokyo"},
    }
    await dispatcher.aclose()
    await client.aclose()


class FakeTransport:
    async def send_message(self, frame: object) -> None:
        raise AssertionError(f"unexpected Daily message: {frame!r}")
