from __future__ import annotations

import pytest

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
        groq_api_key="test-groq",
        anthropic_api_key="test-anthropic",
        otel_endpoint="",
        otel_headers="",
        honeycomb_api_key="test-honeycomb",
        default_llm="groq",
    )
