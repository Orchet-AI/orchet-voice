from __future__ import annotations

import math

from voice.obs.cost import (
    DAILY_CLOUD_USD_PER_MINUTE,
    DEEPGRAM_SPEECH_USD_PER_MINUTE,
    SARVAM_STT_USD_PER_MINUTE,
    SARVAM_TTS_USD_PER_10K_CHARS,
    VoiceSessionCostTracker,
)


def test_cost_estimator_combines_groq_deepgram_and_daily_costs() -> None:
    tracker = VoiceSessionCostTracker(llm_provider="groq")
    tracker.record_llm_tokens_out(1_000_000)

    estimate = tracker.estimate(
        duration_minutes=10.0,
        stt_provider="deepgram",
        tts_provider="deepgram",
    )

    expected = 0.79 + (10.0 * DEEPGRAM_SPEECH_USD_PER_MINUTE) + (10.0 * DAILY_CLOUD_USD_PER_MINUTE)
    assert math.isclose(estimate.estimated_cost_usd, expected, rel_tol=0.0001)
    assert math.isclose(estimate.cost_per_voice_minute_usd, expected / 10.0)


def test_cost_estimator_combines_anthropic_sarvam_and_daily_costs() -> None:
    tracker = VoiceSessionCostTracker(llm_provider="anthropic")
    tracker.record_llm_tokens_out(100_000)
    tracker.record_tts_chars(10_000)

    estimate = tracker.estimate(
        duration_minutes=5.0,
        stt_provider="sarvam",
        tts_provider="sarvam",
    )

    expected = (
        1.5
        + (5.0 * SARVAM_STT_USD_PER_MINUTE)
        + SARVAM_TTS_USD_PER_10K_CHARS
        + (5.0 * DAILY_CLOUD_USD_PER_MINUTE)
    )
    assert math.isclose(estimate.estimated_cost_usd, expected, rel_tol=0.0001)
