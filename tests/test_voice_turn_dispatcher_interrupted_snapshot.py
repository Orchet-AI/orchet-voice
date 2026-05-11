from __future__ import annotations

import asyncio
import json

import httpx

from tests.test_pipeline_helpers import FakeTracer
from voice.pipeline import TTS_STREAM_SPAN_NAME, VoiceMetadata, VoiceTurnTracker, _now
from voice.voice_turn_dispatcher import VoiceTurnDispatcher


async def test_voice_turn_dispatcher_interrupted_snapshot(
    fake_tracer: FakeTracer,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"outcome": "executed", "result": {}})

    client = httpx.AsyncClient(
        base_url="https://api.orchet.ai",
        transport=httpx.MockTransport(handler),
    )
    metadata = VoiceMetadata(voice_session_id="voice_test", user_id="user_test")
    tracker = VoiceTurnTracker(metadata)
    dispatcher = VoiceTurnDispatcher(
        gateway_url="https://api.orchet.ai",
        internal_token="test-internal",
        metadata=metadata,
        tracker=tracker,
        http_client=client,
    )
    tracker.set_snapshot_dispatcher(dispatcher)
    turn = tracker.start_turn("turn_snapshot")
    turn.user_transcript = "what about London"
    turn.assistant_text = "Tokyo is currently"
    turn.tts_span = tracker.start_stage_span(TTS_STREAM_SPAN_NAME)
    turn.tts_started_at = _now() - 0.05

    tracker.interrupt_active_spans()
    await asyncio.sleep(0)

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["interrupted"] is True
    assert payload["session_id"] == "voice_test"
    assert payload["turn_id"] == "turn_snapshot"
    assert payload["user_text"] == "what about London"
    assert payload["assistant_partial_text"] == "Tokyo is currently"
    assert isinstance(payload["cancel_at_ms"], int)
    assert payload["cancel_at_ms"] >= 50
    await dispatcher.aclose()
    await client.aclose()
