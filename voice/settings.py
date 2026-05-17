from __future__ import annotations

import os
from dataclasses import dataclass

from voice import __version__


@dataclass(frozen=True)
class Settings:
    environment: str
    region: str
    version: str
    gateway_url: str
    internal_token: str
    supabase_url: str
    supabase_anon_key: str
    daily_api_key: str
    daily_room_domain: str
    lumo_deepgram_api_key: str
    sarvam_api_key: str
    groq_api_key: str
    anthropic_api_key: str
    openai_api_key: str
    otel_endpoint: str
    otel_headers: str
    honeycomb_api_key: str
    default_llm: str
    voice_stt_model: str
    voice_stt_endpointing_ms: int
    voice_sarvam_stt_model: str
    voice_language_detection_seconds: float
    voice_llm_model: str
    voice_anthropic_model: str
    voice_openai_model: str
    voice_llm_max_tokens: int
    voice_llm_temperature: float
    voice_tts_voice: str
    voice_tts_sample_rate: int
    voice_tts_encoding: str
    voice_sarvam_tts_model: str
    voice_sarvam_tts_speaker: str
    # "streaming" → DeepgramStreamingTTSService (Aura-2 WebSocket, default)
    # "rest"      → pipecat.services.deepgram.DeepgramTTSService (REST fallback)
    # Kept as a kill-switch so we can roll back without redeploy if the
    # streaming adapter misbehaves in production.
    voice_deepgram_tts_mode: str
    # Brain (orchet-brain on Modal) — direct URL + shared JWT secret.
    # Used for the Phase 1 session-context fetch and any future direct
    # brain calls. When unset the BrainMemoryAdapter fails open and
    # voice falls back to the base locale prompt.
    # Defaulted so the existing dataclass call sites (tests, conftest)
    # don't need updating; production env reads override via from_env.
    orchet_ml_brain_url: str = ""
    lumo_ml_service_jwt_secret: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.getenv("ORCHET_VOICE_ENV", "dev"),
            region=os.getenv("ORCHET_VOICE_REGION") or os.getenv("FLY_REGION") or "iad",
            version=__version__,
            gateway_url=os.getenv("ORCHET_GATEWAY_URL", "https://api.orchet.ai"),
            internal_token=os.getenv("ORCHET_INTERNAL_TOKEN", ""),
            supabase_url=os.getenv("NEXT_PUBLIC_SUPABASE_URL", ""),
            supabase_anon_key=os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""),
            daily_api_key=os.getenv("DAILY_API_KEY", ""),
            daily_room_domain=os.getenv("DAILY_ROOM_DOMAIN", "orchet.daily.co"),
            lumo_deepgram_api_key=os.getenv("LUMO_DEEPGRAM_API_KEY", ""),
            sarvam_api_key=os.getenv("SARVAM_API_KEY", ""),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            otel_endpoint=os.getenv("LUMO_OTEL_ENDPOINT", ""),
            otel_headers=os.getenv("LUMO_OTEL_HEADERS", ""),
            honeycomb_api_key=os.getenv("ORCHET_HONEYCOMB_API_KEY", ""),
            # Defaults aligned with production: Claude Haiku 4.5 via
            # Anthropic is the voice LLM. The env vars still override
            # for staging / per-agent experiments; only the fallback
            # changes here so local dev matches what's actually
            # running in prod.
            default_llm=os.getenv("ORCHET_VOICE_LLM_DEFAULT", "anthropic"),
            voice_stt_model=os.getenv("ORCHET_VOICE_STT_MODEL", "nova-3"),
            voice_stt_endpointing_ms=_int_env("ORCHET_VOICE_STT_ENDPOINTING_MS", 300),
            voice_sarvam_stt_model=os.getenv("ORCHET_VOICE_SARVAM_STT_MODEL", "saarika:v2.5"),
            voice_language_detection_seconds=_float_env(
                "ORCHET_VOICE_LANGUAGE_DETECTION_SECONDS", 0.75
            ),
            voice_llm_model=os.getenv("ORCHET_VOICE_LLM_MODEL", "llama-3.3-70b-versatile"),
            voice_anthropic_model=os.getenv("ORCHET_VOICE_ANTHROPIC_MODEL", "claude-haiku-4-5"),
            voice_openai_model=os.getenv("ORCHET_VOICE_OPENAI_MODEL", "gpt-4o-mini"),
            voice_llm_max_tokens=_int_env("ORCHET_VOICE_LLM_MAX_TOKENS", 250),
            voice_llm_temperature=_float_env("ORCHET_VOICE_LLM_TEMPERATURE", 0.7),
            voice_tts_voice=os.getenv("ORCHET_VOICE_TTS_VOICE", "aura-2-andromeda-en"),
            voice_tts_sample_rate=_int_env("ORCHET_VOICE_TTS_SAMPLE_RATE", 24000),
            voice_tts_encoding=os.getenv("ORCHET_VOICE_TTS_ENCODING", "linear16"),
            voice_sarvam_tts_model=os.getenv("ORCHET_VOICE_SARVAM_TTS_MODEL", "bulbul:v3-beta"),
            voice_sarvam_tts_speaker=os.getenv("ORCHET_VOICE_SARVAM_TTS_SPEAKER", "aditya"),
            voice_deepgram_tts_mode=os.getenv("ORCHET_VOICE_DEEPGRAM_TTS_MODE", "streaming"),
            orchet_ml_brain_url=os.getenv("ORCHET_ML_BRAIN_URL", "")
            or os.getenv("LUMO_ML_AGENT_URL", ""),
            lumo_ml_service_jwt_secret=os.getenv("LUMO_ML_SERVICE_JWT_SECRET", ""),
        )

    @property
    def required_secret_names(self) -> tuple[str, ...]:
        return (
            "ANTHROPIC_API_KEY",
            "DAILY_API_KEY",
            "DAILY_ROOM_DOMAIN",
            "GROQ_API_KEY",
            "LUMO_DEEPGRAM_API_KEY",
            "LUMO_OTEL_ENDPOINT",
            "LUMO_OTEL_HEADERS",
            "NEXT_PUBLIC_SUPABASE_ANON_KEY",
            "NEXT_PUBLIC_SUPABASE_URL",
            "ORCHET_GATEWAY_URL",
            "ORCHET_HONEYCOMB_API_KEY",
            "ORCHET_INTERNAL_TOKEN",
            "ORCHET_VOICE_ENV",
            "ORCHET_VOICE_LLM_DEFAULT",
            "SARVAM_API_KEY",
        )

    def health_checks(self) -> dict[str, str]:
        return {
            "deepgram_reachable": _configured(self.lumo_deepgram_api_key),
            "sarvam_reachable": _configured(self.sarvam_api_key),
            "daily_reachable": _configured(self.daily_api_key and self.daily_room_domain),
            "supabase_jwt_validator": _configured(self.supabase_url and self.supabase_anon_key),
            "honeycomb_exporter": _configured(self.otel_endpoint and self.otel_headers),
        }


def _configured(value: object) -> str:
    return "ok" if bool(value) else "missing"


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)
