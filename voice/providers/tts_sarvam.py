from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Callable
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

SARVAM_TTS_WS_URL = "wss://api.sarvam.ai/text-to-speech/ws"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3-beta"
DEFAULT_SARVAM_TTS_SPEAKER = "aditya"


class SarvamTTSService(TTSService):
    """Sarvam Bulbul streaming TTS adapter with Pipecat's TTSService surface."""

    def __init__(
        self,
        *,
        api_key: str,
        target_language_code: str | Callable[[], str],
        model: str = DEFAULT_SARVAM_TTS_MODEL,
        speaker: str = DEFAULT_SARVAM_TTS_SPEAKER,
        sample_rate: int = 24000,
        output_audio_codec: str = "linear16",
        pace: float = 1.0,
        **kwargs: Any,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._api_key = api_key
        self._target_language_code = target_language_code
        self._model = model
        self._output_audio_codec = output_audio_codec
        self._pace = pace
        self._settings = {
            "language": target_language_code,
            "model": model,
            "output_audio_codec": output_audio_codec,
        }
        self.set_voice(speaker)

    @property
    def target_language_code(self) -> str:
        if callable(self._target_language_code):
            return self._target_language_code()
        return self._target_language_code

    @property
    def websocket_url(self) -> str:
        return build_sarvam_tts_ws_url(model=self._model)

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating Sarvam TTS [{text}]")
        try:
            await self.start_ttfb_metrics()
            async with websockets.connect(
                self.websocket_url,
                extra_headers={"api-subscription-key": self._api_key},
                ping_interval=20,
                ping_timeout=20,
            ) as connection:
                await connection.send(json.dumps(self.config_message()))
                await connection.send(json.dumps({"type": "text", "data": {"text": text}}))
                await connection.send(json.dumps({"type": "flush"}))

                await self.start_tts_usage_metrics(text)
                yield TTSStartedFrame()

                async for raw_message in connection:
                    message = parse_sarvam_tts_message(raw_message)
                    if message.error:
                        yield ErrorFrame(message.error)
                        return
                    if message.audio:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(
                            audio=message.audio,
                            sample_rate=self.sample_rate,
                            num_channels=1,
                        )
                    if message.final:
                        yield TTSStoppedFrame()
                        return
        except Exception as exc:
            logger.exception(f"{self} exception: {exc}")
            yield ErrorFrame(f"Error getting Sarvam audio: {exc}")

    def config_message(self) -> dict[str, object]:
        return {
            "type": "config",
            "data": {
                "model": self._model,
                "target_language_code": self.target_language_code,
                "speaker": self._voice_id,
                "speech_sample_rate": str(self.sample_rate or self._init_sample_rate or 24000),
                "output_audio_codec": self._output_audio_codec,
                "pace": self._pace,
                "enable_preprocessing": True,
                "min_buffer_size": 50,
                "max_chunk_length": 150,
            },
        }


class SarvamTTSMessage:
    def __init__(self, *, audio: bytes = b"", final: bool = False, error: str | None = None):
        self.audio = audio
        self.final = final
        self.error = error


def parse_sarvam_tts_message(raw_message: str | bytes) -> SarvamTTSMessage:
    payload = json.loads(raw_message)
    if not isinstance(payload, dict):
        return SarvamTTSMessage()
    data = payload.get("data")
    if not isinstance(data, dict):
        return SarvamTTSMessage()
    if payload.get("type") == "error":
        return SarvamTTSMessage(error=str(data.get("message") or payload))
    if payload.get("type") == "event" and data.get("event_type") == "final":
        return SarvamTTSMessage(final=True)
    if payload.get("type") == "audio":
        audio = data.get("audio")
        if not isinstance(audio, str):
            return SarvamTTSMessage()
        return SarvamTTSMessage(audio=base64.b64decode(audio))
    return SarvamTTSMessage()


def build_sarvam_tts_ws_url(*, model: str) -> str:
    query = urlencode({"model": model, "send_completion_event": "true"})
    return f"{SARVAM_TTS_WS_URL}?{query}"
