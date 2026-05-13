from __future__ import annotations

import pytest

import voice.pipeline as pipeline
from tests.test_pipeline_helpers import FakeTracer
from voice.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        region="iad",
        version="0.1.0",
        gateway_url="https://api.orchet.ai",
        internal_token="test-internal",
        supabase_url="https://orchet-test.supabase.co",
        supabase_anon_key="test-anon",
        daily_api_key="test-daily",
        daily_room_domain="orchet.daily.co",
        lumo_deepgram_api_key="test-deepgram",
        sarvam_api_key="test-sarvam",
        groq_api_key="test-groq",
        anthropic_api_key="test-anthropic",
        openai_api_key="test-openai",
        otel_endpoint="",
        otel_headers="",
        honeycomb_api_key="test-honeycomb",
        default_llm="groq",
        voice_stt_model="nova-3",
        voice_stt_endpointing_ms=300,
        voice_sarvam_stt_model="saarika:v2.5",
        voice_language_detection_seconds=2.0,
        voice_llm_model="llama-3.3-70b-versatile",
        voice_anthropic_model="claude-sonnet-4-6",
        voice_openai_model="gpt-4o-mini",
        voice_llm_max_tokens=250,
        voice_llm_temperature=0.7,
        voice_tts_voice="aura-2-andromeda-en",
        voice_tts_sample_rate=24000,
        voice_tts_encoding="linear16",
        voice_sarvam_tts_model="bulbul:v3-beta",
        voice_sarvam_tts_speaker="aditya",
        voice_deepgram_tts_mode="streaming",
    )


@pytest.fixture
def fake_tracer(monkeypatch: pytest.MonkeyPatch) -> FakeTracer:
    tracer = FakeTracer()
    monkeypatch.setattr(pipeline, "get_tracer", lambda: tracer)
    return tracer
