"""Deepgram Aura-2 streaming WebSocket TTS adapter (persistent connection).

Why this exists
---------------
Pipecat 0.0.61 ships ``pipecat.services.deepgram.DeepgramTTSService``, which
calls Deepgram's REST ``/v1/speak`` endpoint once per synthesis. That works
for single-shot synthesis but in our streaming voice pipeline produces two
distinct user-visible problems:

1. **Choppy word-by-word audio** when ``aggregate_sentences=False`` — each
   per-LLM-token TextFrame triggers a fresh REST call, and every chunk
   arrives with fade-in / fade-out envelope artifacts plus a queue gap
   between calls. Verified in prod 2026-05-13: user reported "for every
   word it's pausing and speaking. TTS is breaking."

2. **25-second mouth-to-ear** when ``aggregate_sentences=True`` — Pipecat
   buffers in ``SimpleTextAggregator`` until a sentence-ending '.!?' is
   matched OR until ``LLMFullResponseEndFrame`` fires; only then does the
   single REST call go out, then the synthesized blob streams to Daily.

Both states forced the user to either tolerate broken audio or wait
half a minute for the bot to start replying — neither acceptable for live
conversation.

This module fixes both by talking to Deepgram's Aura-2 streaming TTS
WebSocket (``wss://api.deepgram.com/v1/speak``): text streams in via
``Speak`` messages, audio streams out continuously, and the model
smooths inter-token transitions internally so no REST boundary artifacts
appear.

Persistent connection (PR-this)
-------------------------------
Earlier iteration opened a fresh WebSocket per ``run_tts`` call. That
solved the choppy / latency-buffering issues but still paid TLS + WS
upgrade handshake (~300-500ms over BOM→US) on every turn. Honeycomb
trace ``voice_96cdb7d48e114f54a0a365f2107f3c01`` after that ship
showed ``voice.total.mouth_to_ear`` averaging 6.0s — better than the
25s regression but with room to compress.

This revision keeps one WebSocket open for the entire voice session.
A long-lived reader task pumps inbound frames into a per-segment
``asyncio.Queue``; each ``run_tts`` invocation acquires a lock, sends
``Speak`` + ``Flush``, then drains the queue until it sees the
terminal ``Flushed`` control frame. Pipecat already serializes
``run_tts`` calls so at most one segment is in flight at a time —
inbound audio between two ``Flushed`` events unambiguously belongs to
the most recently flushed segment.

Lifecycle is wired through ``TTSService.start`` / ``stop`` / ``cancel``:
the connection opens lazily on first use, closes on either graceful
``EndFrame`` or terminal ``CancelFrame``. If the WebSocket drops
mid-session (network blip, server-initiated disconnect), the next
``run_tts`` call transparently reconnects — the user sees one slightly
slower turn, not a dead session.

Protocol summary
----------------
Connect with header ``Authorization: Token <api_key>``. Model /
encoding / sample_rate / container are query params.

Client → server (text JSON frames)::

    {"type": "Speak",  "text": "Hello there"}   # append text to buffer
    {"type": "Flush"}                           # mark end of utterance
    {"type": "Clear"}                           # interrupt (drop buffer)
    {"type": "Close"}                           # graceful shutdown

Server → client::

    <binary>                                    # raw audio frames in
                                                # requested encoding
    {"type": "Metadata",   "request_id": ...}   # informational
    {"type": "Flushed",    "sequence_id": ...}  # buffer drained
    {"type": "Warning",    "description": ...}  # advisory
    {"type": "Error",      "description": ...}  # terminal failure
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

import websockets
from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_services import TTSService

DEEPGRAM_TTS_WS_URL = "wss://api.deepgram.com/v1/speak"
DEFAULT_DEEPGRAM_TTS_VOICE = "aura-2-andromeda-en"
DEFAULT_DEEPGRAM_TTS_ENCODING = "linear16"
DEFAULT_DEEPGRAM_TTS_SAMPLE_RATE = 24000

# Hard ceiling on how long we wait for a queue item between Speak/Flush
# and the next Flushed acknowledgement. A truly stuck connection would
# hang the pipeline forever otherwise — better to surface ErrorFrame
# and let the caller move on (or reconnect on the next turn).
_FLUSH_WAIT_TIMEOUT_S = 10.0


# Sentinel placed on the segment queue by the reader task when the
# upstream WebSocket closes. Lets the consumer distinguish "no more
# audio for this segment" from "server replied with bytes worth zero".
class _Closed:
    __slots__ = ()


_CLOSED_SENTINEL = _Closed()


class DeepgramStreamingTTSService(TTSService):
    """Deepgram Aura-2 streaming TTS over a persistent WebSocket.

    Drop-in replacement for :class:`pipecat.services.deepgram.DeepgramTTSService`
    that holds one WebSocket open across the whole voice session. The
    constructor surface (``api_key``, ``voice``, ``sample_rate``,
    ``encoding``) matches the REST adapter so it remains feature-flag
    swappable from ``transport.py``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice: str = DEFAULT_DEEPGRAM_TTS_VOICE,
        sample_rate: int = DEFAULT_DEEPGRAM_TTS_SAMPLE_RATE,
        encoding: str = DEFAULT_DEEPGRAM_TTS_ENCODING,
        **kwargs: Any,
    ) -> None:
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._api_key = api_key
        self._encoding = encoding
        self._settings = {
            "model": voice,
            "encoding": encoding,
        }
        self.set_voice(voice)
        self._connection = _PersistentDeepgramTTSConnection(
            api_key=api_key,
            url_builder=lambda: self.websocket_url,
        )

    @property
    def websocket_url(self) -> str:
        return build_deepgram_tts_ws_url(
            model=self._voice_id,
            encoding=self._encoding,
            sample_rate=self.sample_rate or DEFAULT_DEEPGRAM_TTS_SAMPLE_RATE,
        )

    def can_generate_metrics(self) -> bool:
        return True

    async def stop(self, frame: EndFrame) -> None:
        """Graceful pipeline shutdown — release the persistent WS."""
        try:
            await self._connection.aclose()
        finally:
            await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        """Terminal cancel (error, user disconnect) — release the WS."""
        try:
            await self._connection.aclose()
        finally:
            await super().cancel(frame)

    async def process_frame(
        self,
        frame: Frame,
        direction: FrameDirection,
    ) -> None:
        """Intercept ``StartInterruptionFrame`` to abort in-flight synthesis.

        When the user barges in, the orchestrator emits a
        ``StartInterruptionFrame``. The base TTSService resets its text
        aggregator on that frame, but Deepgram doesn't know — without a
        ``Clear`` message it would keep streaming the old segment's
        audio bytes for several hundred milliseconds, which would either
        overlap the new turn or pollute its queue. Sending ``Clear``
        tells Deepgram to drop the current synthesis state immediately.
        """
        if isinstance(frame, StartInterruptionFrame):
            # Fire and forget — don't block the interruption path on the
            # network. If the Clear doesn't land, worst case is a brief
            # audio overlap; that's strictly better than blocking the
            # interruption handler.
            asyncio.create_task(self._connection.send_clear())
        await super().process_frame(frame, direction)

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating Deepgram streaming TTS [{text}]")
        try:
            await self.start_ttfb_metrics()
            await self.start_tts_usage_metrics(text)
            yield TTSStartedFrame()

            first_audio_seen = False
            async for item in self._connection.synthesize(text):
                if isinstance(item, (bytes, bytearray)):
                    if not first_audio_seen:
                        # TTFB stops on the first audible byte — that's
                        # what matters for the user's perceived latency.
                        await self.stop_ttfb_metrics()
                        first_audio_seen = True
                    yield TTSAudioRawFrame(
                        audio=bytes(item),
                        sample_rate=self.sample_rate,
                        num_channels=1,
                    )
                    continue
                # Control message (dict).
                kind = item.get("type") if isinstance(item, dict) else None
                if kind == "Flushed":
                    yield TTSStoppedFrame()
                    return
                if kind in {"Metadata", "Warning"}:
                    logger.debug(
                        "voice.deepgram_streaming_tts.control_message",
                        kind=kind,
                        payload=item,
                    )
        except TimeoutError:
            logger.warning(
                "voice.deepgram_streaming_tts.flush_timeout",
                text_len=len(text),
            )
            # Drop the connection so the next turn opens a fresh one.
            # A timed-out connection is almost certainly half-dead.
            await self._connection.aclose()
            yield ErrorFrame("Deepgram TTS flush timeout")
        except _SegmentError as exc:
            logger.warning(
                "voice.deepgram_streaming_tts.segment_error",
                error=str(exc),
            )
            await self._connection.aclose()
            yield ErrorFrame(f"Deepgram TTS error: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"{self} exception: {exc}")
            await self._connection.aclose()
            yield ErrorFrame(f"Error getting Deepgram audio: {exc}")


class _SegmentError(Exception):
    """Raised by the persistent connection when Deepgram sends an
    ``Error`` control frame OR the WS closes before the current
    segment's ``Flushed`` arrives. Caught by ``run_tts`` to surface as
    an ``ErrorFrame`` and force a reconnect on the next turn."""


class _PersistentDeepgramTTSConnection:
    """One Deepgram Aura-2 WebSocket held open across the voice session.

    Pipecat serializes ``run_tts`` calls (each one is awaited to
    completion before the next starts), so at any moment at most one
    ``Speak/Flush`` segment is in flight. We exploit that to use a
    single segment queue: the reader task pushes inbound frames into
    whichever queue is currently registered, and the active ``synthesize``
    coroutine drains it.

    Reconnection is lazy. If the reader task exits because the server
    closed the connection, we mark the connection closed and the next
    call to ``synthesize`` opens a new one transparently.
    """

    def __init__(
        self,
        *,
        api_key: str,
        url_builder: Callable[[], str],
    ) -> None:
        self._api_key = api_key
        self._url_builder = url_builder
        self._connection: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._segment_queue: asyncio.Queue[Any] | None = None
        # Serializes Speak/Flush sends — gates a fresh segment from
        # racing with the previous one's last bytes still in flight.
        self._send_lock = asyncio.Lock()
        # Guards _connection state during open / reconnect.
        self._state_lock = asyncio.Lock()

    async def synthesize(self, text: str) -> AsyncGenerator[Any, None]:
        """Send Speak + Flush, yield inbound frames until Flushed.

        Yields bytes for audio chunks, dicts for control frames
        (Metadata, Warning, Flushed). Raises :class:`_SegmentError` if
        the server emits Error OR the WebSocket closes mid-segment.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        async with self._send_lock:
            await self._ensure_open_locked()
            self._segment_queue = queue
            assert self._connection is not None
            try:
                await self._connection.send(
                    json.dumps({"type": "Speak", "text": text}),
                )
                await self._connection.send(json.dumps({"type": "Flush"}))
            except Exception:
                # Send failed — connection is dead. Clear queue ref so
                # the reader doesn't try to feed it.
                self._segment_queue = None
                raise

        try:
            while True:
                item = await asyncio.wait_for(
                    queue.get(),
                    timeout=_FLUSH_WAIT_TIMEOUT_S,
                )
                if item is _CLOSED_SENTINEL:
                    raise _SegmentError(
                        "WebSocket closed before Flushed",
                    )
                if isinstance(item, dict):
                    if item.get("type") == "Error":
                        raise _SegmentError(
                            str(
                                item.get("description") or item.get("message") or item,
                            ),
                        )
                    yield item
                    if item.get("type") == "Flushed":
                        return
                    continue
                yield item
        finally:
            # Always release the queue reference so the reader stops
            # routing inbound frames to a queue nobody is draining.
            if self._segment_queue is queue:
                self._segment_queue = None

    async def send_clear(self) -> None:
        """Best-effort ``Clear`` for interruption — caller doesn't await
        round-trip, so any send error is swallowed silently."""
        conn = self._connection
        if conn is None:
            return
        try:
            await conn.send(json.dumps({"type": "Clear"}))
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "voice.deepgram_streaming_tts.clear_send_failed",
                error=str(exc)[:200],
            )

    async def aclose(self) -> None:
        """Tear down the WS + reader task. Idempotent."""
        async with self._state_lock:
            conn = self._connection
            reader = self._reader_task
            self._connection = None
            self._reader_task = None
            self._segment_queue = None
        if conn is not None:
            with contextlib.suppress(Exception):
                await conn.send(json.dumps({"type": "Close"}))
            with contextlib.suppress(Exception):
                await conn.close()
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader

    async def _ensure_open_locked(self) -> None:
        """Open the WS + start reader task if not already running.

        Caller MUST hold ``_send_lock`` so we can't race a teardown.
        """
        if self._connection is not None and not self._connection.closed:
            return
        async with self._state_lock:
            # Re-check after acquiring state lock — someone else may have
            # reconnected while we waited.
            if self._connection is not None and not self._connection.closed:
                return
            self._connection = await websockets.connect(
                self._url_builder(),
                extra_headers={"Authorization": f"Token {self._api_key}"},
                ping_interval=20,
                ping_timeout=20,
            )
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Pump inbound frames from the WS into the active segment queue.

        Lives for the lifetime of the connection. Exits on connection
        close or unexpected exception — either way puts a CLOSED sentinel
        in the active queue so the consumer wakes up and unblocks.
        """
        conn = self._connection
        if conn is None:
            return
        try:
            async for raw in conn:
                queue = self._segment_queue
                if queue is None:
                    # No segment in flight — stray inbound frame between
                    # turns. Drop it.
                    continue
                if isinstance(raw, (bytes, bytearray)):
                    await queue.put(bytes(raw))
                    continue
                parsed = _parse_control_message(raw)
                if parsed is not None:
                    await queue.put(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice.deepgram_streaming_tts.reader_loop_exit",
                error=str(exc)[:200],
            )
        finally:
            queue = self._segment_queue
            if queue is not None:
                with contextlib.suppress(asyncio.QueueFull):
                    # Best effort — caller's timeout will catch this.
                    queue.put_nowait(_CLOSED_SENTINEL)


def _parse_control_message(raw: Any) -> dict[str, Any] | None:
    """Best-effort parse of a Deepgram control frame.

    Returns ``None`` for anything we cannot interpret as a JSON object,
    so callers can skip non-JSON server output without crashing.
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        raw = decoded
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def build_deepgram_tts_ws_url(
    *,
    model: str,
    encoding: str,
    sample_rate: int,
) -> str:
    """Build the Deepgram Aura-2 streaming WS URL.

    Free function so it's trivially unit-testable without instantiating
    the service.
    """
    from urllib.parse import urlencode

    query = urlencode(
        {
            "model": model,
            "encoding": encoding,
            "sample_rate": str(sample_rate),
            "container": "none",
        }
    )
    return f"{DEEPGRAM_TTS_WS_URL}?{query}"
