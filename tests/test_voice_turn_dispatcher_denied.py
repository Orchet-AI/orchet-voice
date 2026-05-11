from __future__ import annotations

import httpx

from voice.pipeline import VoiceMetadata, VoiceTurnTracker
from voice.voice_turn_dispatcher import VoiceTurnDispatcher


async def test_voice_turn_dispatcher_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "outcome": "denied",
                "reason_code": "voice_critical_action_denied",
                "voice_message": "I can't do that through voice mode.",
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.orchet.ai",
        transport=httpx.MockTransport(handler),
    )
    metadata = VoiceMetadata(voice_session_id="voice_test", user_id="user_test")
    tracker = VoiceTurnTracker(metadata)
    tracker.start_turn("turn_denied")
    dispatcher = VoiceTurnDispatcher(
        gateway_url="https://api.orchet.ai",
        internal_token="test-internal",
        metadata=metadata,
        tracker=tracker,
        http_client=client,
    )

    outcome = await dispatcher.dispatch("charge_card", {"amount": 50}, transport=FakeTransport())

    assert outcome.function_result == {
        "denied": True,
        "reason_code": "voice_critical_action_denied",
    }
    assert outcome.spoken_text == "I can't do that through voice mode."
    assert outcome.run_llm is False
    await dispatcher.aclose()
    await client.aclose()


class FakeTransport:
    async def send_message(self, frame: object) -> None:
        raise AssertionError(f"unexpected Daily message: {frame!r}")
