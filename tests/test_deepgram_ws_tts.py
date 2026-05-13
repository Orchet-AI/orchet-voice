"""Unit tests for DeepgramStreamingTTSService + the persistent
WebSocket connection that backs it.

These tests do not hit the real Deepgram endpoint. We inject a fake
``connection`` object that exposes the same surface as the
``websockets`` client (``send``, ``close``, ``closed``, async-iterable
yielding inbound frames). The ``_PersistentDeepgramTTSConnection`` is
constructed directly with a builder that produces this fake — bypassing
the real ``websockets.connect`` call.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pipecat.frames.frames import (
    ErrorFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)

from voice.providers import tts_deepgram_ws as module
from voice.providers.tts_deepgram_ws import (
    DEEPGRAM_TTS_WS_URL,
    DeepgramStreamingTTSService,
    _parse_control_message,
    _PersistentDeepgramTTSConnection,
    build_deepgram_tts_ws_url,
)

# ----- URL + control-message parser ----------------------------------


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


def test_service_websocket_url_uses_voice_from_config() -> None:
    """Pipecat's TTSService base owns ``self.sample_rate`` and may rebind
    it via PipelineParams (``audio_out_sample_rate``) before the service
    is actually used, so we don't pin a specific sample_rate value here.
    The build_deepgram_tts_ws_url test above already verifies the
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


# ----- Fake connection -----------------------------------------------


class FakeConnection:
    """Programmable async-iterable stand-in for a ``websockets`` client.

    Tests feed inbound frames via ``inbox.put(...)`` and observe outbound
    sends via the ``sent`` list. The async-iterator pulls from ``inbox``
    until ``closed`` flips to True (or ``StopAsyncIteration`` is signalled
    via a sentinel).
    """

    _STOP = object()

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed: bool = False
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        if self.closed:
            raise ConnectionError("send on closed fake connection")
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True
        await self.inbox.put(self._STOP)

    async def signal_eof(self) -> None:
        """End the async-iterator (server-initiated close) WITHOUT
        marking the connection as send-rejecting. Lets tests simulate
        "server closed the read side mid-segment" while still allowing
        the consumer's already-sent Speak/Flush to have gone through."""
        await self.inbox.put(self._STOP)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Any]:
        while True:
            item = await self.inbox.get()
            if item is self._STOP:
                return
            yield item


def _make_persistent_connection(
    fake: FakeConnection,
) -> _PersistentDeepgramTTSConnection:
    """Build a persistent connection wired to a fake WS.

    We patch ``websockets.connect`` for the scope of this call to return
    the fake. The reader task is started immediately by the consumer
    call path.
    """

    async def fake_connect(*_args: Any, **_kwargs: Any) -> Any:
        return fake

    # Substitute the websockets.connect call inside the module.
    module.websockets.connect = fake_connect  # type: ignore[attr-defined,assignment]
    return _PersistentDeepgramTTSConnection(
        api_key="test-key",
        url_builder=lambda: "wss://fake",
    )


# ----- Persistent connection: happy paths ----------------------------


@pytest.mark.asyncio
async def test_synthesize_yields_audio_then_flushed() -> None:
    fake = FakeConnection()
    conn = _make_persistent_connection(fake)

    audio_a = b"\x01" * 320
    audio_b = b"\x02" * 320

    # Pre-load inbound frames the server "would" send back.
    await fake.inbox.put(audio_a)
    await fake.inbox.put(audio_b)
    await fake.inbox.put(json.dumps({"type": "Flushed"}))

    items: list[Any] = []
    async for item in conn.synthesize("hello there"):
        items.append(item)

    # Both audio bytes arrive in order, then the Flushed control frame.
    assert items[0] == audio_a
    assert items[1] == audio_b
    assert isinstance(items[2], dict) and items[2]["type"] == "Flushed"
    # We sent Speak + Flush on the wire.
    sent = [json.loads(s) for s in fake.sent]
    assert sent[0] == {"type": "Speak", "text": "hello there"}
    assert sent[1] == {"type": "Flush"}

    await conn.aclose()


@pytest.mark.asyncio
async def test_two_synthesize_calls_share_one_websocket() -> None:
    """The whole point of this PR: a second run_tts in the same session
    must NOT open a fresh WS — it should reuse the existing connection,
    saving the TLS handshake cost."""
    fake = FakeConnection()
    conn = _make_persistent_connection(fake)

    open_count = 0
    original_connect = module.websockets.connect

    async def counting_connect(*args: Any, **kwargs: Any) -> FakeConnection:
        nonlocal open_count
        open_count += 1
        return await original_connect(*args, **kwargs)

    module.websockets.connect = counting_connect  # type: ignore[attr-defined]

    # First segment.
    await fake.inbox.put(b"\xaa" * 320)
    await fake.inbox.put(json.dumps({"type": "Flushed"}))
    items1 = [x async for x in conn.synthesize("first")]
    assert any(isinstance(x, bytes) for x in items1)

    # Second segment on the SAME connection.
    await fake.inbox.put(b"\xbb" * 320)
    await fake.inbox.put(json.dumps({"type": "Flushed"}))
    items2 = [x async for x in conn.synthesize("second")]
    assert any(isinstance(x, bytes) for x in items2)

    assert open_count == 1, f"Expected one WS open across two segments, got {open_count}"
    await conn.aclose()


@pytest.mark.asyncio
async def test_synthesize_raises_segment_error_on_server_error_message() -> None:
    fake = FakeConnection()
    conn = _make_persistent_connection(fake)

    await fake.inbox.put(json.dumps({"type": "Error", "description": "rate-limited"}))

    with pytest.raises(module._SegmentError) as exc_info:
        async for _ in conn.synthesize("anything"):
            pass
    assert "rate-limited" in str(exc_info.value).lower()

    await conn.aclose()


@pytest.mark.asyncio
async def test_synthesize_raises_segment_error_on_ws_close_before_flushed() -> None:
    fake = FakeConnection()
    conn = _make_persistent_connection(fake)

    # One audio frame in transit, then the server closes the read side
    # before sending Flushed. signal_eof leaves ``closed`` False so the
    # Speak/Flush send during ``synthesize`` setup still succeeds — what
    # we're testing is "WS dies AFTER we sent Speak but BEFORE Flushed",
    # not "send to a dead WS".
    await fake.inbox.put(b"\xcc" * 320)
    await fake.signal_eof()

    items_seen_before_error: list[Any] = []
    with pytest.raises(module._SegmentError) as exc_info:
        async for item in conn.synthesize("hello"):
            items_seen_before_error.append(item)
    assert "closed before flushed" in str(exc_info.value).lower()
    assert len(items_seen_before_error) == 1
    assert items_seen_before_error[0] == b"\xcc" * 320


@pytest.mark.asyncio
async def test_send_clear_swallows_errors_silently() -> None:
    """``send_clear`` runs on interruption — must never raise into the
    interruption handler. If the WS is dead, log and continue."""
    fake = FakeConnection()
    fake.closed = True  # send will raise ConnectionError
    conn = _PersistentDeepgramTTSConnection(
        api_key="test-key",
        url_builder=lambda: "wss://fake",
    )
    conn._connection = fake  # type: ignore[attr-defined]

    # Should NOT raise.
    await conn.send_clear()
    await conn.aclose()


# ----- Service-level integration via the persistent connection -------


@pytest.mark.asyncio
async def test_run_tts_emits_started_audio_stopped_in_order() -> None:
    fake = FakeConnection()
    service = DeepgramStreamingTTSService(
        api_key="test-key",
        voice="aura-2-andromeda-en",
        sample_rate=24000,
        encoding="linear16",
    )
    # Replace the persistent connection with one wired to our fake.
    service._connection = _make_persistent_connection(fake)  # type: ignore[attr-defined]

    audio = b"\xee" * 320
    await fake.inbox.put(audio)
    await fake.inbox.put(json.dumps({"type": "Flushed"}))

    frames: list[Any] = []
    async for frame in service.run_tts("hello"):
        frames.append(frame)

    # Expect: Started, AudioRaw, Stopped — exactly in that order.
    assert isinstance(frames[0], TTSStartedFrame)
    assert isinstance(frames[1], TTSAudioRawFrame)
    assert frames[1].audio == audio
    assert frames[1].sample_rate == service.sample_rate
    assert frames[1].num_channels == 1
    assert isinstance(frames[2], TTSStoppedFrame)

    await service._connection.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_tts_emits_error_frame_on_segment_error() -> None:
    fake = FakeConnection()
    service = DeepgramStreamingTTSService(
        api_key="test-key",
        voice="aura-2-andromeda-en",
        sample_rate=24000,
        encoding="linear16",
    )
    service._connection = _make_persistent_connection(fake)  # type: ignore[attr-defined]

    await fake.inbox.put(
        json.dumps({"type": "Error", "description": "model overloaded"}),
    )

    frames: list[Any] = []
    async for frame in service.run_tts("hello"):
        frames.append(frame)

    # We always emit TTSStartedFrame first, then ErrorFrame.
    assert isinstance(frames[0], TTSStartedFrame)
    assert any(isinstance(f, ErrorFrame) for f in frames)

    # After an error the service should have dropped its connection so
    # the next turn opens a fresh one. (See run_tts error handlers.)
    assert service._connection._connection is None  # type: ignore[attr-defined]
