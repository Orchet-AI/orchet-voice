"""Deepgram Aura-2 streaming WebSocket TTS adapter.

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
   Verified in prod 2026-05-13 trace
   ``voice_3f09eeac5e6f4cd3ae58a42bfd18ab47``:
   ``voice.total.mouth_to_ear = 25.4 s``.

Neither knob position is acceptable for live conversation. The proper fix
is to consume Deepgram's Aura-2 streaming TTS WebSocket
(``wss://api.deepgram.com/v1/speak``), which:

* accepts incremental text input via ``Speak`` messages,
* streams audio out continuously as the model synthesises, and
* smooths inter-token transitions internally (no REST boundary
  artifacts).

Protocol summary
----------------
Connect with header ``Authorization: Token <api_key>``. The endpoint
takes the model / encoding / sample_rate / container as query params.

Client → server (text JSON frames)::

    {"type": "Speak",  "text": "Hello there"}   # append text to buffer
    {"type": "Flush"}                           # mark end of utterance
    {"type": "Clear"}                           # interrupt (drop buffer)
    {"type": "Close"}                           # graceful shutdown

Server → client::

    <binary>                                    # raw audio frames in the
                                                # requested encoding
    {"type": "Metadata",   "request_id": ...}   # informational
    {"type": "Flushed",    "sequence_id": ...}  # buffer drained
    {"type": "Warning",    "description": ...}  # advisory
    {"type": "Error",      "description": ...}  # terminal failure

Lifecycle
---------
``TTSService`` base calls ``run_tts(text)`` per dispatch unit (per token
with ``aggregate_sentences=False``, per sentence with ``=True``). We
implement one WebSocket session **per** ``run_tts`` invocation rather
than persisting a connection across the session. That keeps the surface
identical to ``SarvamTTSService`` and avoids the invasive ``TTSService``
overrides that a persistent connection would require. The win over the
REST adapter still applies: text-in / audio-out streaming smooths
inter-token transitions inside a single ``run_tts`` call, eliminating the
choppy boundary artifacts.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import urlencode

import websockets
from loguru import logger
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.services.ai_services import TTSService

DEEPGRAM_TTS_WS_URL = "wss://api.deepgram.com/v1/speak"
DEFAULT_DEEPGRAM_TTS_VOICE = "aura-2-andromeda-en"
DEFAULT_DEEPGRAM_TTS_ENCODING = "linear16"
DEFAULT_DEEPGRAM_TTS_SAMPLE_RATE = 24000

# Hard ceiling on how long we wait for the server to acknowledge a Flush
# after we've sent it. Without this, a half-broken connection would hang
# the pipeline forever — better to emit ErrorFrame and let the caller
# move on.
_FLUSH_WAIT_TIMEOUT_S = 10.0


class DeepgramStreamingTTSService(TTSService):
    """Deepgram Aura-2 streaming TTS over WebSocket.

    Drop-in replacement for :class:`pipecat.services.deepgram.DeepgramTTSService`
    that uses the streaming endpoint instead of REST. Same constructor
    surface (``api_key``, ``voice``, ``sample_rate``, ``encoding``) so it
    can be swapped in / out from a feature flag in ``transport.py``.
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

    @property
    def websocket_url(self) -> str:
        return build_deepgram_tts_ws_url(
            model=self._voice_id,
            encoding=self._encoding,
            sample_rate=self.sample_rate or DEFAULT_DEEPGRAM_TTS_SAMPLE_RATE,
        )

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating Deepgram streaming TTS [{text}]")
        try:
            await self.start_ttfb_metrics()
            async with websockets.connect(
                self.websocket_url,
                extra_headers={"Authorization": f"Token {self._api_key}"},
                ping_interval=20,
                ping_timeout=20,
            ) as connection:
                # Push the text to synthesize, then flush so the server
                # knows there is no more input coming on this utterance.
                # Deepgram begins streaming audio back immediately on
                # Speak; Flush only marks the segment boundary and elicits
                # the terminal Flushed control frame so we know when to
                # close.
                await connection.send(json.dumps({"type": "Speak", "text": text}))
                await connection.send(json.dumps({"type": "Flush"}))

                await self.start_tts_usage_metrics(text)
                yield TTSStartedFrame()

                async for frame in _consume_messages(self, connection):
                    yield frame
        except TimeoutError:
            logger.warning(
                "voice.deepgram_streaming_tts.flush_timeout",
                text_len=len(text),
            )
            yield ErrorFrame("Deepgram TTS flush timeout")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"{self} exception: {exc}")
            yield ErrorFrame(f"Error getting Deepgram audio: {exc}")


async def _consume_messages(
    service: DeepgramStreamingTTSService,
    connection: Any,
) -> AsyncGenerator[Frame, None]:
    """Pump messages off the WebSocket until we see ``Flushed`` or error.

    Pulled out into a module function so it can be unit-tested with a
    fake connection (any iterable that yields ``bytes`` / ``str``). The
    real ``connection`` is the ``websockets`` client; iteration over it
    yields each inbound frame.
    """
    received_flushed = False
    async for raw in _with_timeout(connection, _FLUSH_WAIT_TIMEOUT_S):
        if isinstance(raw, (bytes, bytearray)):
            # Stop the time-to-first-byte metric on the first audio
            # frame, not when the WS opens — TTFB matters for the user's
            # perceived first audible byte, not for control plane.
            await service.stop_ttfb_metrics()
            yield TTSAudioRawFrame(
                audio=bytes(raw),
                sample_rate=service.sample_rate,
                num_channels=1,
            )
            continue

        message = _parse_control_message(raw)
        if message is None:
            continue
        kind = message.get("type")
        if kind == "Flushed":
            received_flushed = True
            yield TTSStoppedFrame()
            return
        if kind == "Error":
            yield ErrorFrame(
                str(message.get("description") or message.get("message") or message),
            )
            return
        # Metadata / Warning frames are advisory — ignore for now but
        # surface to logs so we can spot regressions in production.
        if kind in {"Warning", "Metadata"}:
            logger.debug(
                "voice.deepgram_streaming_tts.control_message",
                kind=kind,
                payload=message,
            )

    if not received_flushed:
        # Server closed without acknowledging the flush — surface as
        # error so the pipeline doesn't silently lose this turn.
        yield ErrorFrame("Deepgram TTS WebSocket closed before Flushed")


async def _with_timeout(connection: Any, timeout_s: float) -> AsyncGenerator[Any, None]:
    """Wrap async iteration over the connection with a per-frame timeout.

    ``websockets``' async iterator can hang indefinitely if the server
    stops sending. We wrap each ``__anext__`` in :func:`asyncio.wait_for`
    so a stuck connection becomes an explicit ``TimeoutError`` instead
    of a forever-pending coroutine.
    """
    iterator = connection.__aiter__()
    while True:
        try:
            raw = await asyncio.wait_for(iterator.__anext__(), timeout=timeout_s)
        except StopAsyncIteration:
            return
        yield raw


def _parse_control_message(raw: Any) -> dict[str, Any] | None:
    """Best-effort parse of a Deepgram control frame.

    Returns ``None`` for anything we cannot interpret as a JSON object so
    the consumer can skip it without crashing on malformed input.
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

    Pulled out as a free function so it's trivially unit-testable
    without instantiating the service.
    """
    query = urlencode(
        {
            "model": model,
            "encoding": encoding,
            "sample_rate": str(sample_rate),
            "container": "none",
        }
    )
    return f"{DEEPGRAM_TTS_WS_URL}?{query}"
