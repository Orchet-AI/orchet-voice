from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    StartInterruptionFrame,
    StopInterruptionFrame,
    TranscriptionFrame,
    TransportMessageUrgentFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.obs.cost import VoiceSessionCostTracker
from voice.obs.tracing import get_tracer

STT_STREAM_SPAN_NAME = "voice.stt.stream"
SARVAM_STT_SPAN_NAME = "voice.stt.sarvam"
LLM_STREAM_SPAN_NAME = "voice.llm.stream"
TTS_STREAM_SPAN_NAME = "voice.tts.stream"
SARVAM_TTS_SPAN_NAME = "voice.tts.sarvam"
TOTAL_MOUTH_TO_EAR_SPAN_NAME = "voice.total.mouth_to_ear"
LANG_DETECT_SPAN_NAME = "voice.lang.detect"

CLIENT_KIND_IOS = "ios"
CLIENT_KIND_WEB = "web"


@dataclass(frozen=True)
class VoiceMetadata:
    voice_session_id: str
    user_id: str
    client_kind: str = CLIENT_KIND_WEB
    region: str = "iad"
    locale: str = "unknown"
    agent_id: str = "orchet-super-agent"
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    tts_voice_id: str = "aura-2-andromeda-en"


class InterruptedTurnSnapshotter(Protocol):
    async def snapshot_interrupted(self, snapshot: dict[str, Any]) -> None: ...

    def resolve_confirmation(self, confirmation_id: str, result: str) -> None: ...


@dataclass
class VoiceTurnTiming:
    stt_first_partial_ms: int | None = None
    stt_final_ms: int | None = None
    llm_ttft_ms: int | None = None
    tts_first_chunk_ms: int | None = None


@dataclass
class VoiceTurn:
    turn_id: str
    turn_index: int
    started_at: float
    stt_started_at: float
    llm_started_at: float | None = None
    tts_started_at: float | None = None
    partial_count: int = 0
    llm_tokens_out: int = 0
    tts_total_chars: int = 0
    user_transcript: str = ""
    assistant_text: str = ""
    timing: VoiceTurnTiming = field(default_factory=VoiceTurnTiming)
    total_span: Span | None = None
    stt_span: Span | None = None
    llm_span: Span | None = None
    tts_span: Span | None = None
    total_span_ended: bool = False


class VoiceTurnTracker:
    def __init__(
        self,
        metadata: VoiceMetadata,
        snapshot_dispatcher: InterruptedTurnSnapshotter | None = None,
    ):
        self._metadata = metadata
        self._tracer = get_tracer()
        self._turn: VoiceTurn | None = None
        self._turn_index = 0
        self._snapshot_dispatcher = snapshot_dispatcher
        self._locale = metadata.locale or "unknown"
        self._stt_provider = "deepgram"
        self._tts_provider = "deepgram"
        self._tts_voice_id = metadata.tts_voice_id

    @property
    def current(self) -> VoiceTurn | None:
        return self._turn

    @property
    def locale(self) -> str:
        return self._locale

    @property
    def stt_provider(self) -> str:
        return self._stt_provider

    @property
    def tts_provider(self) -> str:
        return self._tts_provider

    @property
    def tts_voice_id(self) -> str:
        return self._tts_voice_id

    @property
    def stt_span_name(self) -> str:
        return SARVAM_STT_SPAN_NAME if self._stt_provider == "sarvam" else STT_STREAM_SPAN_NAME

    @property
    def tts_span_name(self) -> str:
        return SARVAM_TTS_SPAN_NAME if self._tts_provider == "sarvam" else TTS_STREAM_SPAN_NAME

    def set_snapshot_dispatcher(
        self, snapshot_dispatcher: InterruptedTurnSnapshotter | None
    ) -> None:
        self._snapshot_dispatcher = snapshot_dispatcher

    def set_locale(
        self,
        locale: str,
        *,
        stt_provider: str | None = None,
        tts_provider: str | None = None,
        tts_voice_id: str | None = None,
    ) -> None:
        self._locale = locale or "unknown"
        if stt_provider:
            self._stt_provider = stt_provider
        if tts_provider:
            self._tts_provider = tts_provider
        if tts_voice_id:
            self._tts_voice_id = tts_voice_id
        self._apply_locale_to_active_spans()

    def record_language_detection(
        self,
        *,
        locale: str,
        confidence: float,
        elapsed_ms: int,
        provider: str,
        stt_provider: str,
        tts_provider: str,
        tts_voice_id: str | None = None,
    ) -> None:
        self.set_locale(
            locale,
            stt_provider=stt_provider,
            tts_provider=tts_provider,
            tts_voice_id=tts_voice_id,
        )
        turn = self.ensure_turn()
        parent = trace.set_span_in_context(turn.total_span) if turn.total_span else None
        span = self._tracer.start_span(LANG_DETECT_SPAN_NAME, context=parent)
        self._set_common_attributes(span, turn)
        span.set_attribute("voice.detect.confidence", confidence)
        span.set_attribute("voice.detect.elapsed_ms", elapsed_ms)
        span.set_attribute("voice.detect.provider", provider)
        span.end()

    def start_turn(
        self, turn_id: str | None = None, *, started_at: float | None = None
    ) -> VoiceTurn:
        now = started_at or _now()
        if self._turn and not self._turn.total_span_ended:
            self.finish_total_span(now, interrupted=True)
        self._turn_index += 1
        self._turn = VoiceTurn(
            turn_id=turn_id or _new_turn_id(),
            turn_index=self._turn_index,
            started_at=now,
            stt_started_at=now,
        )
        self._turn.total_span = self._start_span(TOTAL_MOUTH_TO_EAR_SPAN_NAME, self._turn)
        return self._turn

    def ensure_turn(self) -> VoiceTurn:
        if not self._turn:
            return self.start_turn()
        return self._turn

    def start_stage_span(self, span_name: str) -> Span:
        turn = self.ensure_turn()
        parent = trace.set_span_in_context(turn.total_span) if turn.total_span else None
        span = self._tracer.start_span(span_name, context=parent)
        self._set_common_attributes(span, turn)
        return span

    def finish_total_span(
        self, ended_at: float | None = None, *, interrupted: bool = False
    ) -> None:
        turn = self._turn
        if not turn or not turn.total_span or turn.total_span_ended:
            return

        if interrupted:
            turn.total_span.set_attribute("voice.interrupted", True)
            turn.total_span.set_status(Status(StatusCode.UNSET, "interrupted"))
        else:
            turn.total_span.set_attribute(
                "voice.total.first_audio_ms", _elapsed_ms(turn.started_at, ended_at)
            )
        turn.total_span.end()
        turn.total_span_ended = True

    def interrupt_active_spans(self) -> None:
        turn = self._turn
        if not turn:
            return
        barge_in_ms: int | None = None
        if turn.tts_span and turn.tts_span.is_recording() and turn.tts_started_at:
            barge_in_ms = _elapsed_ms(turn.tts_started_at)
            turn.tts_span.set_attribute("voice.tts.barge_in_ms", barge_in_ms)
        if turn.total_span and not turn.total_span_ended and barge_in_ms is not None:
            turn.total_span.set_attribute("voice.tts.barge_in_ms", barge_in_ms)
        self._snapshot_interrupted_turn(turn, barge_in_ms)
        for span in (turn.stt_span, turn.llm_span, turn.tts_span):
            if span and span.is_recording():
                span.set_attribute("voice.interrupted", True)
                span.end()
        self.finish_total_span(interrupted=True)

    def _snapshot_interrupted_turn(self, turn: VoiceTurn, barge_in_ms: int | None) -> None:
        if not self._snapshot_dispatcher:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._snapshot_dispatcher.snapshot_interrupted(
                {
                    "session_id": self._metadata.voice_session_id,
                    "turn_id": turn.turn_id,
                    "turn_index": turn.turn_index,
                    "user_id": self._metadata.user_id,
                    "channel": "voice",
                    "interrupted": True,
                    "user_text": turn.user_transcript,
                    "assistant_partial_text": turn.assistant_text,
                    "cancel_at_ms": barge_in_ms,
                }
            ),
            name=f"voice-interrupted-snapshot-{turn.turn_id}",
        )

    def _start_span(self, span_name: str, turn: VoiceTurn) -> Span:
        span = self._tracer.start_span(span_name)
        self._set_common_attributes(span, turn)
        return span

    def _set_common_attributes(self, span: Span, turn: VoiceTurn) -> None:
        span.set_attribute("voice.session_id", self._metadata.voice_session_id)
        span.set_attribute("voice.turn_id", turn.turn_id)
        span.set_attribute("client.kind", self._metadata.client_kind)
        span.set_attribute("voice.locale", self._locale)

    def _apply_locale_to_active_spans(self) -> None:
        turn = self._turn
        if not turn:
            return
        for span in (turn.total_span, turn.stt_span, turn.llm_span, turn.tts_span):
            if span and span.is_recording():
                span.set_attribute("voice.locale", self._locale)


class ClientVADInterruptionProcessor(FrameProcessor):
    def __init__(
        self,
        tracker: VoiceTurnTracker,
        dispatcher: InterruptedTurnSnapshotter | None = None,
    ):
        super().__init__(name="orchet-client-vad-interruption")
        self._tracker = tracker
        self._dispatcher = dispatcher

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TransportMessageUrgentFrame):
            confirmation = parse_confirmation_resolved_event(frame.message)
            if confirmation:
                if self._dispatcher:
                    self._dispatcher.resolve_confirmation(
                        confirmation["confirmation_id"], confirmation["result"]
                    )
                text = confirmation.get("voice_continuation_hint")
                if confirmation["result"] == "cancelled" and not text:
                    text = "No problem, I've cancelled that."
                if text:
                    await self.push_frame(TTSTextFrame(text), FrameDirection.DOWNSTREAM)
                return

            event = parse_client_vad_event(frame.message)
            if event and event["state"] == "speech_started":
                self._tracker.interrupt_active_spans()
                await self.push_frame(StartInterruptionFrame(), FrameDirection.DOWNSTREAM)
                self._tracker.start_turn(str(event.get("turn_id") or _new_turn_id()))
                await self.push_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
                return
            if event and event["state"] == "speech_ended":
                await self.push_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
                await self.push_frame(StopInterruptionFrame(), FrameDirection.DOWNSTREAM)
                return

        await self.push_frame(frame, direction)


class AudioDurationCostProcessor(FrameProcessor):
    def __init__(self, cost_tracker: VoiceSessionCostTracker):
        super().__init__(name="orchet-audio-duration-cost")
        self._cost_tracker = cost_tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InputAudioRawFrame):
            self._cost_tracker.record_input_audio_frame(
                byte_count=len(frame.audio),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )

        await self.push_frame(frame, direction)


class STTSpanProcessor(FrameProcessor):
    def __init__(self, tracker: VoiceTurnTracker):
        super().__init__(name="orchet-stt-span")
        self._tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            turn = self._tracker.ensure_turn()
            if not turn.stt_span or not turn.stt_span.is_recording():
                turn.stt_started_at = _now()
                turn.stt_span = self._tracker.start_stage_span(self._tracker.stt_span_name)
                turn.stt_span.set_attribute("voice.stt.provider", self._tracker.stt_provider)
        elif isinstance(frame, InterimTranscriptionFrame):
            self._handle_interim(frame)
        elif isinstance(frame, TranscriptionFrame):
            self._handle_final(frame)

        await self.push_frame(frame, direction)

    def _handle_interim(self, frame: InterimTranscriptionFrame) -> None:
        if not frame.text.strip():
            return
        turn = self._tracker.ensure_turn()
        if not turn.stt_span or not turn.stt_span.is_recording():
            turn.stt_span = self._tracker.start_stage_span(self._tracker.stt_span_name)
            turn.stt_span.set_attribute("voice.stt.provider", self._tracker.stt_provider)
        turn.partial_count += 1
        if turn.timing.stt_first_partial_ms is None:
            turn.timing.stt_first_partial_ms = _elapsed_ms(turn.stt_started_at)

    def _handle_final(self, frame: TranscriptionFrame) -> None:
        text = frame.text.strip()
        if not text:
            return
        turn = self._tracker.ensure_turn()
        if not turn.stt_span or not turn.stt_span.is_recording():
            turn.stt_span = self._tracker.start_stage_span(self._tracker.stt_span_name)
            turn.stt_span.set_attribute("voice.stt.provider", self._tracker.stt_provider)

        turn.user_transcript = f"{turn.user_transcript} {text}".strip()
        turn.timing.stt_final_ms = _elapsed_ms(turn.stt_started_at)
        turn.stt_span.set_attribute(
            "voice.stt.first_partial_ms", turn.timing.stt_first_partial_ms or 0
        )
        turn.stt_span.set_attribute("voice.stt.final_ms", turn.timing.stt_final_ms)
        turn.stt_span.set_attribute("voice.stt.partial_count", turn.partial_count)
        turn.stt_span.set_attribute("voice.stt.provider", self._tracker.stt_provider)
        turn.stt_span.end()


class LLMSpanProcessor(FrameProcessor):
    def __init__(
        self,
        tracker: VoiceTurnTracker,
        metadata: VoiceMetadata,
        cost_tracker: VoiceSessionCostTracker | None = None,
    ):
        super().__init__(name="orchet-llm-span")
        self._tracker = tracker
        self._metadata = metadata
        self._cost_tracker = cost_tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartInterruptionFrame):
            self._tracker.interrupt_active_spans()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._handle_llm_start()
        elif isinstance(frame, LLMTextFrame):
            self._handle_llm_text(frame)
        elif isinstance(frame, MetricsFrame):
            self._handle_metrics(frame)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._handle_llm_end()

        await self.push_frame(frame, direction)

    def _handle_llm_start(self) -> None:
        turn = self._tracker.ensure_turn()
        turn.llm_started_at = _now()
        turn.assistant_text = ""
        turn.llm_tokens_out = 0
        turn.llm_span = self._tracker.start_stage_span(LLM_STREAM_SPAN_NAME)
        turn.llm_span.set_attribute("voice.llm.provider", self._metadata.llm_provider)
        turn.llm_span.set_attribute("voice.llm.model", self._metadata.llm_model)

    def _handle_llm_text(self, frame: LLMTextFrame) -> None:
        turn = self._tracker.ensure_turn()
        if not turn.llm_span or not turn.llm_span.is_recording():
            self._handle_llm_start()
        turn.assistant_text += frame.text
        if turn.timing.llm_ttft_ms is None:
            turn.timing.llm_ttft_ms = _elapsed_ms(turn.llm_started_at or turn.started_at)

    def _handle_metrics(self, frame: MetricsFrame) -> None:
        turn = self._tracker.current
        if not turn:
            return
        for item in frame.data:
            if isinstance(item, LLMUsageMetricsData):
                turn.llm_tokens_out = item.value.completion_tokens

    def _handle_llm_end(self) -> None:
        turn = self._tracker.current
        if not turn or not turn.llm_span:
            return

        if turn.llm_tokens_out == 0:
            turn.llm_tokens_out = _estimate_tokens(turn.assistant_text)
        turn.llm_span.set_attribute("voice.llm.ttft_ms", turn.timing.llm_ttft_ms or 0)
        turn.llm_span.set_attribute("voice.llm.total_tokens_out", turn.llm_tokens_out)
        turn.llm_span.set_attribute("voice.llm.provider", self._metadata.llm_provider)
        turn.llm_span.set_attribute("voice.llm.model", self._metadata.llm_model)
        if self._cost_tracker:
            self._cost_tracker.record_llm_tokens_out(turn.llm_tokens_out)
        turn.llm_span.end()


class MarkdownStripperProcessor(FrameProcessor):
    """Strip markdown characters from LLM text before TTS reads them literally.

    Deepgram (and Sarvam) TTS read `**` as "star star", `_` as "underscore",
    and backticks as "backtick". Claude 4.5 defaults to markdown for
    emphasis even when the system prompt forbids it; relying on prompt
    instructions alone leaves "I'm star star sure star star" leaking
    into the audio. We strip the markdown chars deterministically right
    before TTS so the user hears clean prose regardless of model drift.

    LLM text streams in token fragments (e.g. "**" might come as two
    separate "*" chunks), so multi-char regex patterns aren't reliable
    on a per-frame basis. Stripping the individual markdown chars
    `*`, `_`, `` ` `` is equivalent in effect and works on every frame
    in isolation, no aggregation required. Numbered list prefixes like
    "1. " are left alone — TTS reads them naturally as "one,".
    """

    _STRIP_CHARS = str.maketrans("", "", "*_`")

    def __init__(self) -> None:
        super().__init__(name="orchet-markdown-stripper")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMTextFrame) and frame.text:
            cleaned = frame.text.translate(self._STRIP_CHARS)
            if cleaned != frame.text:
                frame.text = cleaned
        await self.push_frame(frame, direction)


class TTSSpanProcessor(FrameProcessor):
    def __init__(
        self,
        tracker: VoiceTurnTracker,
        metadata: VoiceMetadata,
        cost_tracker: VoiceSessionCostTracker | None = None,
    ):
        super().__init__(name="orchet-tts-span")
        self._tracker = tracker
        self._metadata = metadata
        self._cost_tracker = cost_tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartInterruptionFrame):
            self._tracker.interrupt_active_spans()
        elif isinstance(frame, TTSStartedFrame):
            self._handle_tts_start()
        elif isinstance(frame, TTSAudioRawFrame):
            self._handle_tts_audio(frame)
        elif isinstance(frame, TTSTextFrame):
            self._handle_tts_text(frame)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._handle_tts_end()

        await self.push_frame(frame, direction)

    def _handle_tts_start(self) -> None:
        turn = self._tracker.ensure_turn()
        if not turn.tts_span or not turn.tts_span.is_recording():
            turn.tts_started_at = _now()
            turn.tts_span = self._tracker.start_stage_span(self._tracker.tts_span_name)
            turn.tts_span.set_attribute("voice.tts.provider", self._tracker.tts_provider)
            turn.tts_span.set_attribute("voice.tts.voice_id", self._tracker.tts_voice_id)

    def _handle_tts_audio(self, frame: TTSAudioRawFrame) -> None:
        turn = self._tracker.ensure_turn()
        if not turn.tts_span or not turn.tts_span.is_recording():
            self._handle_tts_start()
        if self._cost_tracker:
            self._cost_tracker.record_output_audio_frame(
                byte_count=len(frame.audio),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
            )
        if turn.timing.tts_first_chunk_ms is None:
            turn.timing.tts_first_chunk_ms = _elapsed_ms(turn.tts_started_at or turn.started_at)
            self._tracker.finish_total_span()

    def _handle_tts_text(self, frame: TTSTextFrame) -> None:
        turn = self._tracker.ensure_turn()
        turn.tts_total_chars += len(frame.text)

    def _handle_tts_end(self) -> None:
        turn = self._tracker.current
        if not turn or not turn.tts_span or not turn.tts_span.is_recording():
            return

        turn.tts_span.set_attribute("voice.tts.first_chunk_ms", turn.timing.tts_first_chunk_ms or 0)
        turn.tts_span.set_attribute("voice.tts.total_chars", turn.tts_total_chars)
        turn.tts_span.set_attribute("voice.tts.provider", self._tracker.tts_provider)
        turn.tts_span.set_attribute("voice.tts.voice_id", self._tracker.tts_voice_id)
        if self._cost_tracker:
            self._cost_tracker.record_tts_chars(turn.tts_total_chars)
        turn.tts_span.end()


def parse_client_vad_event(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None

    message_type = message.get("type")
    if message_type not in {"barge_in", "voice.vad"}:
        return None

    state = str(message.get("state") or message.get("event") or "").replace("-", "_")
    if state in {"speech_start", "speech_started", "started", "start"}:
        normalized_state = "speech_started"
    elif state in {"speech_end", "speech_ended", "ended", "end"}:
        normalized_state = "speech_ended"
    else:
        return None

    return {
        "state": normalized_state,
        "turn_id": message.get("turn_id"),
    }


def parse_confirmation_resolved_event(message: Any) -> dict[str, str] | None:
    if not isinstance(message, dict):
        return None
    if message.get("type") != "confirmation_resolved":
        return None
    confirmation_id = message.get("confirmation_id")
    result = message.get("result")
    if not isinstance(confirmation_id, str) or result not in {"executed", "cancelled"}:
        return None
    hint = message.get("voice_continuation_hint")
    event = {"confirmation_id": confirmation_id, "result": result}
    if isinstance(hint, str) and hint.strip():
        event["voice_continuation_hint"] = hint.strip()
    return event


def _now() -> float:
    return time.perf_counter()


def _elapsed_ms(started_at: float, ended_at: float | None = None) -> int:
    return max(0, round(((ended_at or _now()) - started_at) * 1000))


def _estimate_tokens(text: str) -> int:
    return max(0, round(len(text.split()) * 1.33))


def _new_turn_id() -> str:
    return f"turn_{uuid4().hex}"
