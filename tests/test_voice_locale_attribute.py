from __future__ import annotations

from typing import cast

from tests.test_pipeline_helpers import FakeSpan, FakeTracer
from voice.pipeline import (
    LANG_DETECT_SPAN_NAME,
    LLM_STREAM_SPAN_NAME,
    SARVAM_STT_SPAN_NAME,
    SARVAM_TTS_SPAN_NAME,
    TOTAL_MOUTH_TO_EAR_SPAN_NAME,
    VoiceMetadata,
    VoiceTurnTracker,
)


def test_voice_locale_attribute_on_every_voice_span(fake_tracer: FakeTracer) -> None:
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    tracker.start_turn("turn_telugu")

    total_span = next(
        span for span in fake_tracer.spans if span.name == TOTAL_MOUTH_TO_EAR_SPAN_NAME
    )
    assert total_span.attributes["voice.locale"] == "unknown"

    tracker.record_language_detection(
        locale="te-IN",
        confidence=0.91,
        elapsed_ms=123,
        provider="sarvam-unknown",
        stt_provider="sarvam",
        tts_provider="sarvam",
        tts_voice_id="aditya",
    )
    stt_span = tracker.start_stage_span(tracker.stt_span_name)
    llm_span = tracker.start_stage_span(LLM_STREAM_SPAN_NAME)
    tts_span = tracker.start_stage_span(tracker.tts_span_name)
    stt_fake_span = cast(FakeSpan, stt_span)
    llm_fake_span = cast(FakeSpan, llm_span)
    tts_fake_span = cast(FakeSpan, tts_span)

    lang_span = next(span for span in fake_tracer.spans if span.name == LANG_DETECT_SPAN_NAME)
    assert lang_span.attributes["voice.locale"] == "te-IN"
    assert lang_span.attributes["voice.detect.confidence"] == 0.91
    assert lang_span.attributes["voice.detect.elapsed_ms"] == 123
    assert lang_span.attributes["voice.detect.provider"] == "sarvam-unknown"

    for span in (total_span, stt_fake_span, llm_fake_span, tts_fake_span):
        assert span.attributes["voice.locale"] == "te-IN"

    assert stt_fake_span.name == SARVAM_STT_SPAN_NAME
    assert tts_fake_span.name == SARVAM_TTS_SPAN_NAME
    assert tracker.tts_voice_id == "aditya"
