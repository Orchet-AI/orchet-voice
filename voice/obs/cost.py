from __future__ import annotations

import time
from dataclasses import dataclass, field

from opentelemetry.trace import Span

LLM_OUTPUT_USD_PER_M_TOKEN = {
    "groq": 0.79,
    "anthropic": 15.00,
    "openai": 0.15,
}

DEEPGRAM_SPEECH_USD_PER_MINUTE = 0.0058
SARVAM_STT_USD_PER_MINUTE = 0.006
SARVAM_TTS_USD_PER_10K_CHARS = 15.0 / 83.0
DAILY_CLOUD_USD_PER_MINUTE = 0.99 / 100.0


@dataclass
class VoiceSessionCostTracker:
    llm_provider: str
    started_at: float = field(default_factory=time.perf_counter)
    llm_tokens_out: int = 0
    tts_total_chars: int = 0
    input_audio_seconds: float = 0.0
    output_audio_seconds: float = 0.0

    def record_llm_tokens_out(self, tokens_out: int) -> None:
        self.llm_tokens_out += max(0, tokens_out)

    def record_tts_chars(self, chars: int) -> None:
        self.tts_total_chars += max(0, chars)

    def record_input_audio_frame(
        self, *, byte_count: int, sample_rate: int, num_channels: int
    ) -> None:
        self.input_audio_seconds += _pcm16_seconds(
            byte_count=byte_count,
            sample_rate=sample_rate,
            num_channels=num_channels,
        )

    def record_output_audio_frame(
        self, *, byte_count: int, sample_rate: int, num_channels: int
    ) -> None:
        self.output_audio_seconds += _pcm16_seconds(
            byte_count=byte_count,
            sample_rate=sample_rate,
            num_channels=num_channels,
        )

    def estimate(
        self,
        *,
        duration_minutes: float | None = None,
        stt_provider: str,
        tts_provider: str,
    ) -> VoiceSessionCostEstimate:
        wall_minutes = max(0.0, (time.perf_counter() - self.started_at) / 60.0)
        if duration_minutes is not None:
            wall_minutes = duration_minutes
        voice_minutes = duration_minutes
        if voice_minutes is None:
            voice_minutes = max(
                self.input_audio_seconds / 60.0,
                self.output_audio_seconds / 60.0,
                wall_minutes,
            )

        llm_rate = LLM_OUTPUT_USD_PER_M_TOKEN.get(
            self.llm_provider, LLM_OUTPUT_USD_PER_M_TOKEN["groq"]
        )
        llm_usd = (self.llm_tokens_out / 1_000_000.0) * llm_rate
        daily_usd = wall_minutes * DAILY_CLOUD_USD_PER_MINUTE

        speech_usd = 0.0
        sarvam_tts_usd = 0.0
        if stt_provider == "sarvam":
            speech_usd += voice_minutes * SARVAM_STT_USD_PER_MINUTE
        if tts_provider == "sarvam":
            sarvam_tts_usd = (self.tts_total_chars / 10_000.0) * SARVAM_TTS_USD_PER_10K_CHARS
            speech_usd += sarvam_tts_usd
        if stt_provider == "deepgram" or tts_provider == "deepgram":
            speech_usd += voice_minutes * DEEPGRAM_SPEECH_USD_PER_MINUTE

        total_usd = llm_usd + speech_usd + daily_usd
        return VoiceSessionCostEstimate(
            duration_minutes=voice_minutes,
            llm_usd=llm_usd,
            speech_usd=speech_usd,
            daily_usd=daily_usd,
            sarvam_tts_usd=sarvam_tts_usd,
            estimated_cost_usd=total_usd,
        )


@dataclass(frozen=True)
class VoiceSessionCostEstimate:
    duration_minutes: float
    llm_usd: float
    speech_usd: float
    daily_usd: float
    sarvam_tts_usd: float
    estimated_cost_usd: float

    @property
    def cost_per_voice_minute_usd(self) -> float:
        if self.duration_minutes <= 0:
            return 0.0
        return self.estimated_cost_usd / self.duration_minutes


def set_cost_span_attributes(
    span: Span,
    *,
    estimate: VoiceSessionCostEstimate,
    llm_provider: str,
    stt_provider: str,
    tts_provider: str,
    locale: str,
) -> None:
    span.set_attribute("voice.session.estimated_cost_usd", estimate.estimated_cost_usd)
    span.set_attribute("voice.session.duration_minutes", estimate.duration_minutes)
    span.set_attribute("voice.session.cost_per_minute_usd", estimate.cost_per_voice_minute_usd)
    span.set_attribute("voice.session.llm_cost_usd", estimate.llm_usd)
    span.set_attribute("voice.session.speech_cost_usd", estimate.speech_usd)
    span.set_attribute("voice.session.daily_cost_usd", estimate.daily_usd)
    span.set_attribute("voice.llm.provider", llm_provider)
    span.set_attribute("voice.stt.provider", stt_provider)
    span.set_attribute("voice.tts.provider", tts_provider)
    span.set_attribute("voice.locale", locale)


def _pcm16_seconds(*, byte_count: int, sample_rate: int, num_channels: int) -> float:
    if byte_count <= 0 or sample_rate <= 0 or num_channels <= 0:
        return 0.0
    return byte_count / float(sample_rate * num_channels * 2)
