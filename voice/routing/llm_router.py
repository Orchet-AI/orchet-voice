from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast

from pipecat.services.anthropic import AnthropicLLMService
from pipecat.services.groq import GroqLLMService
from pipecat.services.openai import BaseOpenAILLMService, OpenAILLMService

from voice.settings import Settings

LLMProvider = Literal["groq", "anthropic", "openai"]

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

DEFAULT_AGENT_LLM_MANIFESTS: dict[str, dict[str, str]] = {
    "lumo-rentals-trip-planner": {
        "llm_preference": "anthropic",
        "llm_model": DEFAULT_ANTHROPIC_MODEL,
    },
    "lumo-rentals-chat": {
        "llm_preference": "groq",
        "llm_model": "llama-3.3-70b-versatile",
    },
    "customer-support": {
        "llm_preference": "anthropic",
        "llm_model": DEFAULT_ANTHROPIC_MODEL,
    },
}


def agent_manifest_for(
    *, agent_id: str, provided_manifest: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    manifest = dict(provided_manifest or {})
    if not manifest:
        manifest = dict(DEFAULT_AGENT_LLM_MANIFESTS.get(agent_id, {}))
    manifest.setdefault("agent_id", agent_id)
    return manifest


def pick_llm_service(
    *,
    agent_manifest: Mapping[str, Any] | None,
    settings: Settings,
) -> Any:
    provider = pick_llm_provider(agent_manifest, settings=settings)
    model = pick_llm_model(agent_manifest, provider=provider, settings=settings)

    if provider == "anthropic":
        service = AnthropicLLMService(
            api_key=settings.anthropic_api_key,
            model=model,
            params=AnthropicLLMService.InputParams(
                max_tokens=settings.voice_llm_max_tokens,
                temperature=settings.voice_llm_temperature,
            ),
        )
    elif provider == "openai":
        service = OpenAILLMService(
            api_key=settings.openai_api_key,
            model=model,
            params=BaseOpenAILLMService.InputParams(
                max_tokens=settings.voice_llm_max_tokens,
                temperature=settings.voice_llm_temperature,
            ),
        )
    else:
        service = GroqLLMService(
            api_key=settings.groq_api_key,
            model=model,
            params=BaseOpenAILLMService.InputParams(
                max_tokens=settings.voice_llm_max_tokens,
                temperature=settings.voice_llm_temperature,
            ),
        )

    service.orchet_llm_provider = provider  # type: ignore[attr-defined]
    service.orchet_llm_model = model  # type: ignore[attr-defined]
    return service


def pick_llm_provider(
    agent_manifest: Mapping[str, Any] | None,
    *,
    settings: Settings,
) -> LLMProvider:
    raw = _string(agent_manifest, "llm_preference") or settings.default_llm
    if raw in {"anthropic", "openai", "groq"}:
        return cast(LLMProvider, raw)
    return "groq"


def pick_llm_model(
    agent_manifest: Mapping[str, Any] | None,
    *,
    provider: LLMProvider,
    settings: Settings,
) -> str:
    manifest_model = _string(agent_manifest, "llm_model")
    if manifest_model:
        return manifest_model
    if provider == "anthropic":
        return settings.voice_anthropic_model
    if provider == "openai":
        return settings.voice_openai_model
    return settings.voice_llm_model


def llm_provider_for(service: object) -> LLMProvider:
    provider = getattr(service, "orchet_llm_provider", "groq")
    if provider in {"anthropic", "openai", "groq"}:
        return cast(LLMProvider, provider)
    return "groq"


def llm_model_for(service: object, *, fallback: str) -> str:
    model = getattr(service, "orchet_llm_model", None)
    return model if isinstance(model, str) and model else fallback


def _string(mapping: Mapping[str, Any] | None, key: str) -> str | None:
    if not mapping:
        return None
    value = mapping.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
