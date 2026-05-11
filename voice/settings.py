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
    groq_api_key: str
    anthropic_api_key: str
    otel_endpoint: str
    otel_headers: str
    honeycomb_api_key: str
    default_llm: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.getenv("ORCHET_VOICE_ENV", "dev"),
            region=os.getenv("ORCHET_VOICE_REGION") or os.getenv("FLY_REGION", "iad"),
            version=__version__,
            gateway_url=os.getenv("ORCHET_GATEWAY_URL", "https://api.orchet.ai"),
            internal_token=os.getenv("ORCHET_INTERNAL_TOKEN", ""),
            supabase_url=os.getenv("NEXT_PUBLIC_SUPABASE_URL", ""),
            supabase_anon_key=os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""),
            daily_api_key=os.getenv("DAILY_API_KEY", ""),
            daily_room_domain=os.getenv("DAILY_ROOM_DOMAIN", "orchet.daily.co"),
            lumo_deepgram_api_key=os.getenv("LUMO_DEEPGRAM_API_KEY", ""),
            groq_api_key=os.getenv("GROQ_API_KEY", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            otel_endpoint=os.getenv("LUMO_OTEL_ENDPOINT", ""),
            otel_headers=os.getenv("LUMO_OTEL_HEADERS", ""),
            honeycomb_api_key=os.getenv("ORCHET_HONEYCOMB_API_KEY", ""),
            default_llm=os.getenv("ORCHET_VOICE_LLM_DEFAULT", "groq"),
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
        )

    def health_checks(self) -> dict[str, str]:
        return {
            "deepgram_reachable": _configured(self.lumo_deepgram_api_key),
            "daily_reachable": _configured(self.daily_api_key and self.daily_room_domain),
            "supabase_jwt_validator": _configured(self.supabase_url and self.supabase_anon_key),
            "honeycomb_exporter": _configured(self.otel_endpoint and self.otel_headers),
        }


def _configured(value: object) -> str:
    return "ok" if bool(value) else "missing"
