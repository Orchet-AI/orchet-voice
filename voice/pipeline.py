from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pipecat.frames.frames import (
    Frame,
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

from voice.obs.tracing import get_tracer
from voice.persistence import DeferredTranscriptPersistence, VoiceTurnTiming, VoiceTurnTranscript

STT_STREAM_SPAN_NAME = "voice.stt.stream"
LLM_STREAM_SPAN_NAME = "voice.llm.stream"
TTS_STREAM_SPAN_NAME = "voice.tts.stream"
TOTAL_MOUTH_TO_EAR_SPAN_NAME = "voice.total.mouth_to_ear"

CLIENT_KIND_IOS = "ios"
CLIENT_KIND_WEB = "web"


@dataclass(frozen=True)
class VoiceMetadata:
    voice_session_id: str
    user_id: str
    client_kind: str = CLIENT_KIND_WEB
    llm_model: str = "llama-3.3-70b-versatile"
    tts_voice_id: str = "aura-2-andromeda-en"


@dataclass
class VoiceTurn:
    turn_id: str
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
    def __init__(self, metadata: VoiceMetadata):
        self._metadata = metadata
        self._tracer = get_tracer()
        self._turn: VoiceTurn | None = None

    @property
    def current(self) -> VoiceTurn | None:
        return self._turn

    def start_turn(
        self, turn_id: str | None = None, *, started_at: float | None = None
    ) -> VoiceTurn:
        now = started_at or _now()
        if self._turn and not self._turn.total_span_ended:
            self.finish_total_span(now, interrupted=True)
        self._turn = VoiceTurn(
            turn_id=turn_id or _new_turn_id(),
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
        for span in (turn.stt_span, turn.llm_span, turn.tts_span):
            if span and span.is_recording():
                span.set_attribute("voice.interrupted", True)
                span.end()
        if turn.total_span and not turn.total_span_ended and barge_in_ms is not None:
            turn.total_span.set_attribute("voice.tts.barge_in_ms", barge_in_ms)
        self.finish_total_span(interrupted=True)

    def _start_span(self, span_name: str, turn: VoiceTurn) -> Span:
        span = self._tracer.start_span(span_name)
        self._set_common_attributes(span, turn)
        return span

    def _set_common_attributes(self, span: Span, turn: VoiceTurn) -> None:
        span.set_attribute("voice.session_id", self._metadata.voice_session_id)
        span.set_attribute("voice.turn_id", turn.turn_id)
        span.set_attribute("client.kind", self._metadata.client_kind)


class ClientVADInterruptionProcessor(FrameProcessor):
    def __init__(self, tracker: VoiceTurnTracker):
        super().__init__(name="orchet-client-vad-interruption")
        self._tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TransportMessageUrgentFrame):
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
                turn.stt_span = self._tracker.start_stage_span(STT_STREAM_SPAN_NAME)
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
            turn.stt_span = self._tracker.start_stage_span(STT_STREAM_SPAN_NAME)
        turn.partial_count += 1
        if turn.timing.stt_first_partial_ms is None:
            turn.timing.stt_first_partial_ms = _elapsed_ms(turn.stt_started_at)

    def _handle_final(self, frame: TranscriptionFrame) -> None:
        text = frame.text.strip()
        if not text:
            return
        turn = self._tracker.ensure_turn()
        if not turn.stt_span or not turn.stt_span.is_recording():
            turn.stt_span = self._tracker.start_stage_span(STT_STREAM_SPAN_NAME)

        turn.user_transcript = f"{turn.user_transcript} {text}".strip()
        turn.timing.stt_final_ms = _elapsed_ms(turn.stt_started_at)
        turn.stt_span.set_attribute(
            "voice.stt.first_partial_ms", turn.timing.stt_first_partial_ms or 0
        )
        turn.stt_span.set_attribute("voice.stt.final_ms", turn.timing.stt_final_ms)
        turn.stt_span.set_attribute("voice.stt.partial_count", turn.partial_count)
        turn.stt_span.end()


class LLMSpanProcessor(FrameProcessor):
    def __init__(
        self,
        tracker: VoiceTurnTracker,
        metadata: VoiceMetadata,
    ):
        super().__init__(name="orchet-llm-span")
        self._tracker = tracker
        self._metadata = metadata

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
        turn.llm_span.set_attribute("voice.llm.provider", "groq")
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
        turn.llm_span.set_attribute("voice.llm.provider", "groq")
        turn.llm_span.set_attribute("voice.llm.model", self._metadata.llm_model)
        turn.llm_span.end()


class TTSSpanProcessor(FrameProcessor):
    def __init__(
        self,
        tracker: VoiceTurnTracker,
        metadata: VoiceMetadata,
        persistence: DeferredTranscriptPersistence,
    ):
        super().__init__(name="orchet-tts-span")
        self._tracker = tracker
        self._metadata = metadata
        self._persistence = persistence

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartInterruptionFrame):
            self._tracker.interrupt_active_spans()
        elif isinstance(frame, TTSStartedFrame):
            self._handle_tts_start()
        elif isinstance(frame, TTSAudioRawFrame):
            self._handle_tts_audio()
        elif isinstance(frame, TTSTextFrame):
            self._handle_tts_text(frame)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._handle_tts_end()

        await self.push_frame(frame, direction)

    def _handle_tts_start(self) -> None:
        turn = self._tracker.ensure_turn()
        if not turn.tts_span or not turn.tts_span.is_recording():
            turn.tts_started_at = _now()
            turn.tts_span = self._tracker.start_stage_span(TTS_STREAM_SPAN_NAME)
            turn.tts_span.set_attribute("voice.tts.provider", "deepgram")
            turn.tts_span.set_attribute("voice.tts.voice_id", self._metadata.tts_voice_id)

    def _handle_tts_audio(self) -> None:
        turn = self._tracker.ensure_turn()
        if not turn.tts_span or not turn.tts_span.is_recording():
            self._handle_tts_start()
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
        turn.tts_span.set_attribute("voice.tts.provider", "deepgram")
        turn.tts_span.set_attribute("voice.tts.voice_id", self._metadata.tts_voice_id)
        turn.tts_span.end()
        self._persistence.schedule(
            VoiceTurnTranscript(
                session_id=self._metadata.voice_session_id,
                turn_id=turn.turn_id,
                user_id=self._metadata.user_id,
                user_text=turn.user_transcript,
                assistant_text=turn.assistant_text.strip(),
                timing=turn.timing,
            )
        )


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


def _now() -> float:
    return time.perf_counter()


def _elapsed_ms(started_at: float, ended_at: float | None = None) -> int:
    return max(0, round(((ended_at or _now()) - started_at) * 1000))


def _estimate_tokens(text: str) -> int:
    return max(0, round(len(text.split()) * 1.33))


def _new_turn_id() -> str:
    return f"turn_{uuid4().hex}"
