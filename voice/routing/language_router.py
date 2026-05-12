from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import websockets
from pipecat.frames.frames import (
    AudioRawFrame,
    Frame,
    InputAudioRawFrame,
    LLMFullResponseEndFrame,
    TextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.pipeline import VoiceTurnTracker
from voice.providers.stt_sarvam import (
    DEFAULT_SARVAM_STT_MODEL,
    build_sarvam_stt_ws_url,
    parse_sarvam_stt_message,
)

ProviderName = Literal["deepgram", "sarvam"]

PROMPT_DIR = Path(__file__).parents[1] / "prompts"
SARVAM_ROUTED_LOCALES = {"hi-IN", "te-IN", "ta-IN"}
ENGLISH_LOCALES = {"en", "en-US", "en-GB", "en-IN"}


@dataclass(frozen=True)
class LanguageDetectionResult:
    locale: str
    confidence: float
    provider: str
    elapsed_ms: int


class AsyncLanguageDetector(Protocol):
    async def detect(
        self, audio_first_seconds: bytes, *, sample_rate: int
    ) -> LanguageDetectionResult: ...


def detect_language(audio_first_seconds: bytes) -> str:
    """Synchronous fallback hook kept for tests and non-streaming probes."""
    if not audio_first_seconds:
        return "en-US"
    return "en-US"


def normalize_locale(detected: str | None) -> str:
    value = (detected or "").strip()
    lower = value.lower().replace("_", "-")
    if lower in {"hinglish", "hi", "hin", "hi-in"}:
        return "hi-IN"
    if lower in {"te", "telugu", "te-in"}:
        return "te-IN"
    if lower in {"ta", "tamil", "ta-in"}:
        return "ta-IN"
    if lower in {"en", "eng", "english", "en-us"}:
        return "en-US"
    if lower == "en-gb":
        return "en-GB"
    if lower == "en-in":
        return "en-IN"
    return value or "en-US"


def pick_stt_provider(detected: str) -> ProviderName:
    locale = normalize_locale(detected)
    if locale in SARVAM_ROUTED_LOCALES:
        return "sarvam"
    return "deepgram"


def pick_tts_provider(detected: str) -> ProviderName:
    return pick_stt_provider(detected)


def sarvam_locale_for(detected: str) -> str:
    locale = normalize_locale(detected)
    return locale if locale in SARVAM_ROUTED_LOCALES else "hi-IN"


def load_voice_prompt(locale: str, *, prompt_dir: Path = PROMPT_DIR) -> str:
    normalized = normalize_locale(locale)
    suffix = normalized.split("-", maxsplit=1)[0]
    candidate = prompt_dir / f"default_voice_{suffix}.txt"
    if candidate.exists():
        return candidate.read_text(encoding="utf-8").strip()
    return (prompt_dir / "default_voice.txt").read_text(encoding="utf-8").strip()


class SarvamStreamingLanguageDetector:
    """Detect language via Sarvam's documented streaming `unknown` language mode."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_SARVAM_STT_MODEL,
        timeout_seconds: float = 4.0,
    ):
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def detect(
        self, audio_first_seconds: bytes, *, sample_rate: int
    ) -> LanguageDetectionResult:
        started = time.perf_counter()
        if not audio_first_seconds:
            return LanguageDetectionResult(
                locale="en-US",
                confidence=0.0,
                provider="sarvam-unknown",
                elapsed_ms=_elapsed_ms(started),
            )

        url = build_sarvam_stt_ws_url(
            language_code="unknown",
            model=self._model,
            sample_rate=sample_rate,
            input_audio_codec="pcm_s16le",
            high_vad_sensitivity=True,
        )
        async with websockets.connect(
            url,
            extra_headers={"api-subscription-key": self._api_key},
            ping_interval=20,
            ping_timeout=20,
        ) as connection:
            await connection.send(
                json.dumps(
                    {
                        "audio": {
                            "data": base64.b64encode(audio_first_seconds).decode("ascii"),
                            "sample_rate": str(sample_rate),
                            "encoding": "audio/wav",
                        }
                    }
                )
            )
            await connection.send(json.dumps({"type": "flush"}))
            deadline = time.perf_counter() + self._timeout_seconds
            while time.perf_counter() < deadline:
                raw = await asyncio.wait_for(
                    connection.recv(),
                    timeout=max(0.1, deadline - time.perf_counter()),
                )
                message = parse_sarvam_stt_message(raw)
                if message.language_code:
                    return LanguageDetectionResult(
                        locale=normalize_locale(message.language_code),
                        confidence=message.language_probability or 0.0,
                        provider="sarvam-unknown",
                        elapsed_ms=_elapsed_ms(started),
                    )
        return LanguageDetectionResult(
            locale=detect_language(audio_first_seconds),
            confidence=0.0,
            provider="fallback",
            elapsed_ms=_elapsed_ms(started),
        )


class LanguageDetectionProcessor(FrameProcessor):
    """Buffers the first audio slice, detects language, then releases audio to STT."""

    def __init__(
        self,
        *,
        tracker: VoiceTurnTracker,
        detector: AsyncLanguageDetector,
        sarvam_tts_speaker: str,
        deepgram_tts_voice: str,
        detection_seconds: float = 2.0,
        on_locale_detected: Callable[[str], None] | None = None,
    ):
        super().__init__(name="orchet-language-detection-router")
        self._tracker = tracker
        self._detector = detector
        self._sarvam_tts_speaker = sarvam_tts_speaker
        self._deepgram_tts_voice = deepgram_tts_voice
        self._detection_seconds = detection_seconds
        self._on_locale_detected = on_locale_detected
        self._audio_buffer: list[InputAudioRawFrame] = []
        self._pending_start: UserStartedSpeakingFrame | None = None
        self._detected_for_turn: str | None = None
        self._released = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, UserStartedSpeakingFrame):
            self._audio_buffer = []
            self._pending_start = frame
            self._detected_for_turn = None
            self._released = False
            return

        if isinstance(frame, InputAudioRawFrame) and not self._released:
            self._audio_buffer.append(frame)
            if self._buffered_seconds() >= self._detection_seconds:
                await self._detect_and_release()
            return

        if isinstance(frame, UserStoppedSpeakingFrame) and not self._released:
            await self._detect_and_release()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _detect_and_release(self) -> None:
        if self._released:
            return
        sample_rate = self._audio_buffer[0].sample_rate if self._audio_buffer else 16000
        audio = b"".join(frame.audio for frame in self._audio_buffer)
        result = await self._detector.detect(audio, sample_rate=sample_rate)
        locale = normalize_locale(result.locale)
        stt_provider = pick_stt_provider(locale)
        tts_provider = pick_tts_provider(locale)
        voice_id = (
            self._sarvam_tts_speaker if tts_provider == "sarvam" else self._deepgram_tts_voice
        )
        self._tracker.record_language_detection(
            locale=locale,
            confidence=result.confidence,
            elapsed_ms=result.elapsed_ms,
            provider=result.provider,
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            tts_voice_id=voice_id,
        )
        if self._on_locale_detected:
            self._on_locale_detected(locale)
        self._detected_for_turn = locale
        self._released = True

        if self._pending_start:
            await self.push_frame(self._pending_start, FrameDirection.DOWNSTREAM)
        for buffered in self._audio_buffer:
            await self.push_frame(buffered, FrameDirection.DOWNSTREAM)
        self._audio_buffer = []
        self._pending_start = None

    def _buffered_seconds(self) -> float:
        if not self._audio_buffer:
            return 0.0
        total_frames = sum(frame.num_frames for frame in self._audio_buffer)
        sample_rate = self._audio_buffer[0].sample_rate or 16000
        return total_frames / sample_rate


class ProviderGateProcessor(FrameProcessor):
    def __init__(
        self,
        *,
        tracker: VoiceTurnTracker,
        provider: ProviderName,
        selected_provider: Callable[[], str],
        gated_types: Iterable[type],
        name: str,
    ):
        super().__init__(name=name)
        self._tracker = tracker
        self._provider = provider
        self._selected_provider = selected_provider
        self._gated_types = tuple(gated_types)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, self._gated_types)
            and self._selected_provider() != self._provider
        ):
            return
        await self.push_frame(frame, direction)


class LanguagePromptProcessor(FrameProcessor):
    def __init__(self, *, tracker: VoiceTurnTracker, context: OpenAILLMContext):
        super().__init__(name="orchet-language-prompt")
        self._tracker = tracker
        self._context = context
        self._last_locale: str | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self._refresh_prompt()
        await self.push_frame(frame, direction)

    def _refresh_prompt(self) -> None:
        locale = self._tracker.locale
        if locale == self._last_locale:
            return
        messages = self._context.get_messages()
        prompt = load_voice_prompt(locale)
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = prompt
        else:
            messages.insert(0, {"role": "system", "name": "system", "content": prompt})
        self._last_locale = locale


def stt_gate_types() -> tuple[type, ...]:
    return (AudioRawFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame)


def tts_gate_types() -> tuple[type, ...]:
    return (TextFrame, TTSSpeakFrame, LLMFullResponseEndFrame)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))
