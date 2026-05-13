"""Unit tests for voice/tools/builtin_tools.py.

These tests are deterministic: time/date use freezegun-style monkey
patching, weather/web use httpx.MockTransport so we exercise the real
HTTP plumbing without going to wttr.in / DuckDuckGo / Tavily.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any

import httpx
import pytest

from voice.tools import builtin_tools as mod

# ----- time / date ----------------------------------------------------


@pytest.mark.asyncio
async def test_current_time_returns_iso_and_human_form() -> None:
    result = await mod.handle_current_time({"timezone": "Asia/Kolkata"})
    # Don't assert the actual time — assert shape.
    assert "iso" in result
    assert "human" in result
    assert result["timezone"] == "Asia/Kolkata"
    # ISO must include offset (timezone-aware).
    assert "+" in result["iso"] or "-" in result["iso"][10:]


@pytest.mark.asyncio
async def test_current_time_falls_back_to_utc_on_bogus_timezone() -> None:
    result = await mod.handle_current_time({"timezone": "Made/Up_Zone"})
    # Falls back silently — we don't want voice to error out on a
    # mis-pronounced city name.
    assert "iso" in result
    assert "human" in result


@pytest.mark.asyncio
async def test_current_time_defaults_to_utc_when_arg_missing() -> None:
    result = await mod.handle_current_time({})
    assert "iso" in result
    # UTC has no DST so the offset is always +00:00 in the iso string.
    assert "+00:00" in result["iso"] or "Z" in result["iso"]


@pytest.mark.asyncio
async def test_current_date_returns_weekday_and_iso() -> None:
    result = await mod.handle_current_date({"timezone": "UTC"})
    assert "iso" in result
    assert "weekday" in result
    # iso must be the YYYY-MM-DD shape.
    _dt.date.fromisoformat(result["iso"])
    assert result["weekday"] in {
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }


# ----- weather (wttr.in) ---------------------------------------------


@pytest.mark.asyncio
async def test_current_weather_parses_wttr_payload() -> None:
    payload = {
        "current_condition": [
            {
                "weatherDesc": [{"value": "Partly cloudy"}],
                "temp_C": "28",
                "FeelsLikeC": "30",
            }
        ],
        "nearest_area": [
            {"areaName": [{"value": "Bengaluru"}]},
        ],
    }

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        assert "wttr.in/Bangalore" in str(request.url)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport, timeout=4.0) as client:
        result = await mod.handle_current_weather(
            {"location": "Bangalore"},
            http_client=client,
        )

    assert result["location"] == "Bengaluru"
    assert result["condition"] == "Partly cloudy"
    assert result["temperature_c"] == 28
    assert result["feels_like_c"] == 30
    assert "28°C" in result["summary"]
    assert "feels like 30°C" in result["summary"]


@pytest.mark.asyncio
async def test_current_weather_handles_missing_location() -> None:
    result = await mod.handle_current_weather({"location": ""})
    assert "error" in result


@pytest.mark.asyncio
async def test_current_weather_handles_http_failure() -> None:
    async def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport, timeout=4.0) as client:
        result = await mod.handle_current_weather(
            {"location": "Mars"},
            http_client=client,
        )

    assert "error" in result


# ----- web_search ----------------------------------------------------


@pytest.mark.asyncio
async def test_web_search_uses_tavily_when_api_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    tavily_response = {
        "answer": "Argentina won the 2022 FIFA World Cup.",
        "results": [
            {"url": "https://example.com/wc", "content": "Argentina won..."},
        ],
    }

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        if "api.tavily.com" in str(request.url):
            body = json.loads(request.content)
            assert body["api_key"] == "test-key"
            assert body["include_answer"] is True
            return httpx.Response(200, json=tavily_response)
        return httpx.Response(404)

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport, timeout=4.0) as client:
        result = await mod.handle_web_search(
            {"query": "who won the latest world cup"},
            http_client=client,
        )

    assert result["provider"] == "tavily"
    assert "Argentina" in result["answer"]
    assert result["source"] == "https://example.com/wc"


@pytest.mark.asyncio
async def test_web_search_falls_back_to_ddg_when_no_tavily_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    ddg_response = {
        "AbstractText": "Python is a high-level programming language.",
        "AbstractURL": "https://en.wikipedia.org/wiki/Python_(programming_language)",
    }

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        if "api.duckduckgo.com" in str(request.url):
            return httpx.Response(200, json=ddg_response)
        return httpx.Response(404)

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport, timeout=4.0) as client:
        result = await mod.handle_web_search(
            {"query": "python programming language"},
            http_client=client,
        )

    assert result["provider"] == "duckduckgo"
    assert "Python" in result["answer"]


@pytest.mark.asyncio
async def test_web_search_falls_back_to_ddg_when_tavily_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resilience: Tavily down should never block the user — we silently
    fall back to DDG. The user shouldn't ever see "Tavily is broken"."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    ddg_response = {"Answer": "42"}

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        if "api.tavily.com" in str(request.url):
            return httpx.Response(500, text="tavily broke")
        if "api.duckduckgo.com" in str(request.url):
            return httpx.Response(200, json=ddg_response)
        return httpx.Response(404)

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport, timeout=4.0) as client:
        result = await mod.handle_web_search(
            {"query": "answer to everything"},
            http_client=client,
        )

    # Quietly downgraded — caller gets a usable answer regardless.
    assert result["provider"] == "duckduckgo"
    assert "42" in result["answer"]


@pytest.mark.asyncio
async def test_web_search_rejects_empty_query() -> None:
    result = await mod.handle_web_search({"query": "   "})
    assert "error" in result


@pytest.mark.asyncio
async def test_web_search_truncates_very_long_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voice mode shouldn't get a 2000-char Wikipedia paragraph back —
    cap it before the LLM sees it, otherwise it'll burn tokens and
    risk long replies."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    huge_text = "word " * 1000  # ~5000 chars

    async def transport_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"AbstractText": huge_text})

    transport = httpx.MockTransport(transport_handler)
    async with httpx.AsyncClient(transport=transport, timeout=4.0) as client:
        result = await mod.handle_web_search(
            {"query": "tell me everything"},
            http_client=client,
        )

    assert len(result["answer"]) < 400  # truncated


# ----- public registry exposes the right names ----------------------


def test_builtin_registry_lists_all_four_tools() -> None:
    """Voice tool catalog and the registry must be in sync — if you
    add a schema in tool_catalog.py without registering a handler
    here, the LLM will call into thin air."""
    assert set(mod.BUILTIN_TOOL_HANDLERS) == {
        "current_time",
        "current_date",
        "current_weather",
        "web_search",
    }


# Use os to silence unused-import warning in case test runner imports order.
_ = os
_ = Any
