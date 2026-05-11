from __future__ import annotations

from pipecat.services.anthropic import AnthropicLLMService
from pipecat.services.groq import GroqLLMService
from pipecat.services.openai import OpenAILLMService

from voice.routing.llm_router import (
    agent_manifest_for,
    llm_model_for,
    llm_provider_for,
    pick_llm_service,
)
from voice.settings import Settings


def test_llm_router_defaults_unknown_agent_to_groq(settings: Settings) -> None:
    manifest = agent_manifest_for(agent_id="unknown-agent")

    service = pick_llm_service(agent_manifest=manifest, settings=settings)

    assert isinstance(service, GroqLLMService)
    assert llm_provider_for(service) == "groq"
    assert llm_model_for(service, fallback="fallback") == "llama-3.3-70b-versatile"


def test_llm_router_picks_anthropic_from_manifest(settings: Settings) -> None:
    service = pick_llm_service(
        agent_manifest={
            "agent_id": "booking-agent",
            "llm_preference": "anthropic",
            "llm_model": "claude-sonnet-4-6",
        },
        settings=settings,
    )

    assert isinstance(service, AnthropicLLMService)
    assert llm_provider_for(service) == "anthropic"
    assert llm_model_for(service, fallback="fallback") == "claude-sonnet-4-6"


def test_llm_router_picks_openai_from_manifest(settings: Settings) -> None:
    service = pick_llm_service(
        agent_manifest={
            "agent_id": "chat-agent",
            "llm_preference": "openai",
            "llm_model": "gpt-4o-mini",
        },
        settings=settings,
    )

    assert isinstance(service, OpenAILLMService)
    assert llm_provider_for(service) == "openai"
    assert llm_model_for(service, fallback="fallback") == "gpt-4o-mini"


def test_llm_router_uses_documented_agent_fallback(settings: Settings) -> None:
    manifest = agent_manifest_for(agent_id="lumo-rentals-trip-planner")

    service = pick_llm_service(agent_manifest=manifest, settings=settings)

    assert isinstance(service, AnthropicLLMService)
    assert llm_provider_for(service) == "anthropic"
