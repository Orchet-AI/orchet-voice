from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from urllib.parse import urlencode

import websockets
from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_services import STTService
from pipecat.utils.time import time_now_iso8601

SARVAM_STT_WS_URL = "wss://api.sarvam.ai/speech-to-text/ws"
DEFAULT_SARVAM_STT_MODEL = "saarika:v2.5"

LanguageDetectedCallback = Callable[[str, float | None], Awaitable[None]]


class SarvamSTTService(STTService):
    """Sarvam Saarika streaming STT adapter with Pipecat's STTService surface."""

    def __init__(
        self,
        *,
        api_key: str,
        target_language_code: str,
        model: str = DEFAULT_SARVAM_STT_MODEL,
        sample_rate: int = 16000,
        input_audio_codec: str = "pcm_s16le",
        high_vad_sensitivity: bool = True,
        on_language_detected: LanguageDetectedCallback | None = None,
        **kwargs: Any,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._api_key = api_key
        self._target_language_code = target_language_code
        self._model = model
        self._input_audio_codec = input_audio_codec
        self._high_vad_sensitivity = high_vad_sensitivity
        self._on_language_detected = on_language_detected
        self._connection: Any | None = None
        self._receiver_task: Any | None = None
        self._settings = {
            "language": target_language_code,
            "model": model,
        }

    @property
    def target_language_code(self) -> str:
        return self._target_language_code

    @property
    def websocket_url(self) -> str:
        return build_sarvam_stt_ws_url(
            language_code=self._target_language_code,
            model=self._model,
            sample_rate=self.sample_rate or self._init_sample_rate or 16000,
            input_audio_codec=self._input_audio_codec,
            high_vad_sensitivity=self._high_vad_sensitivity,
        )

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._disconnect()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStoppedSpeakingFrame):
            await self.flush()

    async def set_language(self, language: Any) -> None:
        self._target_language_code = str(getattr(language, "value", language))
        self._settings["language"] = self._target_language_code
        await self._disconnect()
        await self._connect()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if not self._connection:
            await self._connect()
        if not self._connection:
            yield ErrorFrame("Sarvam STT connection is unavailable")
            return
        await self._connection.send(
            json.dumps(
                {
                    "audio": {
                        "data": base64.b64encode(audio).decode("ascii"),
                        "sample_rate": str(self.sample_rate or self._init_sample_rate or 16000),
                        "encoding": "audio/wav",
                    }
                }
            )
        )
        yield None

    async def flush(self) -> None:
        if self._connection:
            await self._connection.send(json.dumps({"type": "flush"}))

    async def _connect(self) -> None:
        if self._connection:
            return
        logger.debug("Connecting to Sarvam STT")
        self._connection = await websockets.connect(
            self.websocket_url,
            extra_headers={"api-subscription-key": self._api_key},
            ping_interval=20,
            ping_timeout=20,
        )
        self._receiver_task = self.create_task(self._receive_messages(), "sarvam-stt-receiver")

    async def _disconnect(self) -> None:
        task = self._receiver_task
        self._receiver_task = None
        if task:
            await self.cancel_task(task)
        connection = self._connection
        self._connection = None
        if connection:
            await connection.close()

    async def _receive_messages(self) -> None:
        assert self._connection is not None
        async for raw_message in self._connection:
            try:
                frame = parse_sarvam_stt_message(raw_message)
            except Exception as exc:
                await self.push_error(ErrorFrame(f"Error parsing Sarvam STT response: {exc}"))
                continue
            if frame.language_code and self._on_language_detected:
                await self._on_language_detected(frame.language_code, frame.language_probability)
            if frame.error:
                await self.push_error(ErrorFrame(frame.error))
            elif frame.transcript:
                await self.push_frame(
                    TranscriptionFrame(frame.transcript, "", time_now_iso8601()),
                    FrameDirection.DOWNSTREAM,
                )


class SarvamSTTMessage:
    def __init__(
        self,
        *,
        transcript: str = "",
        language_code: str | None = None,
        language_probability: float | None = None,
        error: str | None = None,
    ):
        self.transcript = transcript
        self.language_code = language_code
        self.language_probability = language_probability
        self.error = error


def parse_sarvam_stt_message(raw_message: str | bytes) -> SarvamSTTMessage:
    payload = json.loads(raw_message)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return SarvamSTTMessage()
    if payload.get("type") == "error" or "error" in data:
        return SarvamSTTMessage(error=str(data.get("error") or data.get("message") or payload))
    transcript = data.get("transcript")
    language_code = data.get("language_code")
    return SarvamSTTMessage(
        transcript=transcript.strip() if isinstance(transcript, str) else "",
        language_code=language_code if isinstance(language_code, str) else None,
        language_probability=(
            float(data["language_probability"])
            if isinstance(data.get("language_probability"), int | float)
            else None
        ),
    )


def build_sarvam_stt_ws_url(
    *,
    language_code: str,
    model: str,
    sample_rate: int,
    input_audio_codec: str,
    high_vad_sensitivity: bool,
) -> str:
    query = urlencode(
        {
            "language-code": language_code,
            "model": model,
            "sample_rate": str(sample_rate),
            "input_audio_codec": input_audio_codec,
            "high_vad_sensitivity": str(high_vad_sensitivity).lower(),
            "flush_signal": "true",
            "vad_signals": "false",
        }
    )
    return f"{SARVAM_STT_WS_URL}?{query}"
