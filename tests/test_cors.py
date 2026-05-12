"""CORS preflight tests — voice/server.py must accept browser preflight
from orchet.ai + Vercel preview origins so the streaming voice handshake
works cross-origin from the web client."""

from __future__ import annotations

from fastapi.testclient import TestClient

from voice.server import create_app
from voice.settings import Settings


def _client() -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(
                environment="test",
                region="iad",
                version="0.0.0-test",
                gateway_url="https://api.orchet.ai",
                internal_token="",
                supabase_url="",
                supabase_anon_key="",
                daily_api_key="",
                daily_room_domain="orchet.daily.co",
                lumo_deepgram_api_key="",
                sarvam_api_key="",
                groq_api_key="",
                anthropic_api_key="",
                openai_api_key="",
                otel_endpoint="",
                otel_headers="",
                honeycomb_api_key="",
                default_llm="groq",
                voice_sarvam_stt_model="saarika:v2.5",
                voice_language_detection_seconds=2.0,
                voice_llm_model="llama-3.3-70b-versatile",
                voice_anthropic_model="claude-sonnet-4-6",
                voice_openai_model="gpt-4o-mini",
                voice_llm_max_tokens=250,
                voice_llm_temperature=0.7,
                voice_tts_voice="aura-2-andromeda-en",
                voice_sarvam_tts_speaker="aditya",
                voice_tts_sample_rate=24000,
                voice_tts_encoding="linear16",
            )
        )
    )


def test_cors_preflight_from_orchet_ai_origin() -> None:
    response = _client().options(
        "/debug/echo",
        headers={
            "Origin": "https://orchet.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    # FastAPI's CORSMiddleware short-circuits OPTIONS preflight with 200.
    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin") == "https://orchet.ai"
    allowed_methods = response.headers.get("access-control-allow-methods", "")
    assert "POST" in allowed_methods
    allowed_headers = response.headers.get("access-control-allow-headers", "")
    assert "authorization" in allowed_headers.lower()


def test_cors_preflight_from_vercel_preview_origin() -> None:
    response = _client().options(
        "/debug/echo",
        headers={
            "Origin": "https://lumo-super-agent-abc123-prasanthkalas-6046s-projects.vercel.app",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("access-control-allow-origin", "").endswith(".vercel.app")


def test_cors_blocks_unknown_origin() -> None:
    response = _client().options(
        "/debug/echo",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    # FastAPI/Starlette's CORSMiddleware still returns 200 on bare preflight
    # but omits the allow-origin header for non-matching origins; the browser
    # then refuses the request.
    assert response.headers.get("access-control-allow-origin") != "https://evil.example.com"
