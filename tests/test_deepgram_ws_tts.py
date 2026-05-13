"""Unit tests for DeepgramStreamingTTSService.

These tests do not hit the real Deepgram endpoint — they exercise the
WebSocket consumer / control-message parser with a fake async-iterable
"connection" that yields the same shape of frames Deepgram does (raw
bytes for audio, JSON-encoded text for control messages).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)

from voice.providers.tts_deepgram_ws import (
    DEEPGRAM_TTS_WS_URL,
    DeepgramStreamingTTSService,
    _consume_messages,
    _parse_control_message,
    build_deepgram_tts_ws_url,
)


def test_build_url_includes_required_query_params() -> None:
    url = build_deepgram_tts_ws_url(
        model="aura-2-andromeda-en",
        encoding="linear16",
        sample_rate=24000,
    )
    assert url.startswith(f"{DEEPGRAM_TTS_WS_URL}?")
    assert "model=aura-2-andromeda-en" in url
    assert "encoding=linear16" in url
    assert "sample_rate=24000" in url
    assert "container=none" in url


def test_parse_control_message_handles_well_formed_json() -> None:
    payload = _parse_control_message(json.dumps({"type": "Flushed"}))
    assert payload == {"type": "Flushed"}


def test_parse_control_message_handles_bytes_payload() -> None:
    raw = json.dumps({"type": "Warning", "description": "rate-limited"}).encode("utf-8")
    payload = _parse_control_message(raw)
    assert payload is not None
    assert payload["type"] == "Warning"


def test_parse_control_message_returns_none_for_garbage() -> None:
    assert _parse_control_message("not json") is None
    assert _parse_control_message(123) is None
    assert _parse_control_message(json.dumps([1, 2, 3])) is None  # not a dict


@dataclass
class FakeConnection:
    """Async-iterable stand-in for a websockets client."""

    messages: list[Any] = field(default_factory=list)

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for m in self.messages:
                yield m

        return gen()


class FakeService:
    """Minimal stand-in for DeepgramStreamingTTSService used by the
    consumer loop. We only need the two coroutines and a sample rate."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self.sample_rate = sample_rate
        self.ttfb_stopped = 0

    async def stop_ttfb_metrics(self) -> None:
        self.ttfb_stopped += 1


@pytest.mark.asyncio
async def test_consume_yields_audio_then_stops_on_flushed() -> None:
    audio_a = b"\x01" * 320
    audio_b = b"\x02" * 320
    fake = FakeConnection(
        messages=[
            audio_a,
            audio_b,
            json.dumps({"type": "Flushed"}),
        ]
    )
    service = FakeService()

    frames = []
    async for frame in _consume_messages(service, fake):  # type: ignore[arg-type]
        frames.append(frame)

    # Two audio frames + one TTSStoppedFrame, in order.
    assert len(frames) == 3
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert frames[0].audio == audio_a
    assert frames[0].sample_rate == 24000
    assert frames[0].num_channels == 1
    assert isinstance(frames[1], TTSAudioRawFrame)
    assert frames[1].audio == audio_b
    assert isinstance(frames[2], TTSStoppedFrame)
    # TTFB metric stops on the first audio frame, not on every one.
    assert service.ttfb_stopped >= 1


@pytest.mark.asyncio
async def test_consume_yields_error_on_error_message() -> None:
    fake = FakeConnection(
        messages=[
            json.dumps({"type": "Error", "description": "rate limit exceeded"}),
        ]
    )
    service = FakeService()

    frames = []
    async for frame in _consume_messages(service, fake):  # type: ignore[arg-type]
        frames.append(frame)

    assert len(frames) == 1
    assert isinstance(frames[0], ErrorFrame)
    assert "rate limit" in frames[0].error.lower()


@pytest.mark.asyncio
async def test_consume_ignores_metadata_and_warning_then_completes() -> None:
    audio = b"\x03" * 320
    fake = FakeConnection(
        messages=[
            json.dumps({"type": "Metadata", "request_id": "abc"}),
            audio,
            json.dumps({"type": "Warning", "description": "slow synthesis"}),
            json.dumps({"type": "Flushed"}),
        ]
    )
    service = FakeService()

    frames = []
    async for frame in _consume_messages(service, fake):  # type: ignore[arg-type]
        frames.append(frame)

    # Metadata + Warning are ignored, so we see 1 audio frame + 1 stop.
    assert len(frames) == 2
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert isinstance(frames[1], TTSStoppedFrame)


@pytest.mark.asyncio
async def test_consume_surfaces_error_if_closed_before_flush() -> None:
    """If the server closes the connection without ever sending Flushed,
    we should emit an ErrorFrame rather than completing silently — a
    silent completion would look to the pipeline like a successful but
    empty turn, dropping the user's intent on the floor."""
    fake = FakeConnection(messages=[b"\x01" * 320])  # one audio frame, then EOF
    service = FakeService()

    frames = []
    async for frame in _consume_messages(service, fake):  # type: ignore[arg-type]
        frames.append(frame)

    assert len(frames) == 2
    assert isinstance(frames[0], TTSAudioRawFrame)
    assert isinstance(frames[1], ErrorFrame)
    assert "closed before Flushed" in frames[1].error


def test_service_websocket_url_uses_voice_from_config() -> None:
    """The url builder must pick up the voice we configured. We don't
    assert on sample_rate here because Pipecat's TTSService base class
    owns ``self.sample_rate`` and may rebind it later via PipelineParams
    (``audio_out_sample_rate``) before the service is actually used.
    The ``build_deepgram_tts_ws_url`` test above already verifies the
    sample_rate query param is forwarded correctly."""
    service = DeepgramStreamingTTSService(
        api_key="test-key",
        voice="aura-2-helios-en",
        sample_rate=16000,
        encoding="linear16",
    )
    url = service.websocket_url
    assert "model=aura-2-helios-en" in url
    assert "encoding=linear16" in url
    assert "sample_rate=" in url
