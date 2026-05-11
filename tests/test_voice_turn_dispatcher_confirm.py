from __future__ import annotations

import httpx
from pipecat.frames.frames import TransportMessageUrgentFrame

from voice.pipeline import VoiceMetadata, VoiceTurnTracker
from voice.voice_turn_dispatcher import VoiceTurnDispatcher


async def test_voice_turn_dispatcher_requires_visual_confirmation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "outcome": "requires_visual_confirmation",
                "confirmation_id": "conf_test",
                "voice_message": "Please confirm this flight on screen.",
                "confirmation_payload": {
                    "title": "Confirm booking",
                    "summary": "SFO to NRT",
                    "details": [{"label": "Price", "value": "$850"}],
                    "confirm_action": "duffel_book_flight",
                    "expires_at": "2026-05-12T12:00:00Z",
                },
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.orchet.ai",
        transport=httpx.MockTransport(handler),
    )
    metadata = VoiceMetadata(voice_session_id="voice_test", user_id="user_test")
    tracker = VoiceTurnTracker(metadata)
    tracker.start_turn("turn_confirm")
    transport = FakeTransport()
    dispatcher = VoiceTurnDispatcher(
        gateway_url="https://api.orchet.ai",
        internal_token="test-internal",
        metadata=metadata,
        tracker=tracker,
        http_client=client,
    )

    outcome = await dispatcher.dispatch(
        "duffel_book_flight",
        {"origin": "SFO", "destination": "NRT"},
        transport=transport,
    )

    assert outcome.function_result == {"deferred": True, "confirmation_id": "conf_test"}
    assert outcome.spoken_text == "Please confirm this flight on screen."
    assert outcome.run_llm is False
    assert len(transport.messages) == 1
    daily_message = transport.messages[0].message
    assert daily_message["type"] == "show_confirmation"
    assert daily_message["voice_session_id"] == "voice_test"
    assert daily_message["turn_id"] == "turn_confirm"
    assert daily_message["confirmation_id"] == "conf_test"
    assert daily_message["confirmation_payload"]["summary"] == "SFO to NRT"
    await dispatcher.aclose()
    await client.aclose()


class FakeTransport:
    def __init__(self) -> None:
        self.messages: list[TransportMessageUrgentFrame] = []

    async def send_message(self, frame: TransportMessageUrgentFrame) -> None:
        self.messages.append(frame)
