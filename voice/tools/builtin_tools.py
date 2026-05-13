"""Built-in voice tools that don't require routing to orchet-backend.

These are pragmatic fixes for the most common user-facing capability
gaps surfaced in production (user reported 2026-05-13 "Orchet doesn't
have access to internet or web search. When I asked for weather or
current time it said it doesn't have access to any of them").

The voice LLM (Groq Llama) only knows about tools we put in
``VOICE_TOOLS_SCHEMA``. Without these tools registered, it correctly
says "I don't have internet" — it really doesn't. Path A (this file)
wires the four most-requested tools as **local** handlers in the voice
service itself, so they answer in milliseconds without paying the
``/voice/turn`` round-trip to orchet-backend.

Tools shipped here:

* ``current_time``        — local Python ``datetime.now()`` against the
                            request's IANA timezone (defaults to UTC).
                            Pure compute, no network.
* ``current_date``        — local ``date.today()`` + weekday. No network.
* ``current_weather``     — wttr.in (free, no API key) returns a
                            one-line forecast for a given location.
* ``web_search``          — Tavily Search API when ``TAVILY_API_KEY`` is
                            configured; otherwise DuckDuckGo Instant
                            Answer as a no-key fallback. Returns the
                            top result with a short snippet so the LLM
                            can summarise it for the user.

Path B (route every voice turn through orchet-backend's full MCP
catalog) is the architecturally correct answer and is tracked
separately — these direct handlers are the unblock-now layer.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote_plus

import httpx
import structlog

logger = structlog.get_logger()

# Per-call HTTP timeout. Voice is hard real-time — we'd rather return a
# graceful "couldn't reach the weather service" than block the whole
# turn for 30 seconds.
_HTTP_TIMEOUT_S = 4.0

# Used as the user-agent on wttr.in / DuckDuckGo so they don't drop us
# as a generic Python client.
_USER_AGENT = "orchet-voice/0.1 (+https://orchet.ai)"


BuiltinToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def handle_current_time(args: dict[str, Any]) -> dict[str, Any]:
    """Return the current wall-clock time in the requested IANA zone.

    Argument schema::

        {
          "timezone": "Asia/Kolkata"   # optional, defaults to UTC
        }

    Result schema::

        {
          "iso":      "2026-05-13T14:32:09+05:30",
          "human":    "Wednesday, May 13, 2026 at 2:32 PM IST",
          "timezone": "Asia/Kolkata"
        }
    """
    tz_name = (args.get("timezone") or "UTC").strip()
    tz = _safe_zoneinfo(tz_name)
    now = _dt.datetime.now(tz)
    return {
        "iso": now.isoformat(timespec="seconds"),
        "human": now.strftime("%A, %B %d, %Y at %I:%M %p %Z").replace(" 0", " "),
        "timezone": str(tz) if tz else tz_name,
    }


async def handle_current_date(args: dict[str, Any]) -> dict[str, Any]:
    """Return today's date in the requested IANA zone.

    Argument schema::

        {
          "timezone": "Asia/Kolkata"   # optional, defaults to UTC
        }

    Result schema::

        {
          "iso":     "2026-05-13",
          "weekday": "Wednesday",
          "human":   "Wednesday, May 13, 2026"
        }
    """
    tz_name = (args.get("timezone") or "UTC").strip()
    tz = _safe_zoneinfo(tz_name)
    today = _dt.datetime.now(tz).date()
    return {
        "iso": today.isoformat(),
        "weekday": today.strftime("%A"),
        "human": today.strftime("%A, %B %d, %Y"),
    }


async def handle_current_weather(
    args: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch a one-line weather summary from wttr.in.

    wttr.in is free, requires no API key, and supports both city names
    and lat/lon. We use its ``format=j1`` JSON output and extract the
    fields we actually want to read aloud — overall condition, temp,
    feels-like temp.

    Argument schema::

        {
          "location": "Bangalore"     # city, region, airport, or zip
        }

    Result schema (success)::

        {
          "location":         "Bangalore",
          "condition":        "Partly cloudy",
          "temperature_c":    28,
          "feels_like_c":     30,
          "summary":          "Partly cloudy, 28°C (feels like 30°C)"
        }

    Result schema (failure)::

        {"error": "..."}
    """
    location = (args.get("location") or "").strip()
    if not location:
        return {"error": "Need a location to look up weather for."}

    url = f"https://wttr.in/{quote_plus(location)}?format=j1"
    client = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    owns_client = http_client is None
    try:
        response = await client.get(url, headers={"User-Agent": _USER_AGENT})
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        logger.warning("voice.tools.weather.timeout", location=location)
        return {"error": "Weather service didn't respond in time."}
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice.tools.weather.failed", location=location, error=str(exc)[:200])
        return {"error": f"Couldn't fetch weather for {location}."}
    finally:
        if owns_client:
            await client.aclose()

    current = (payload.get("current_condition") or [None])[0] or {}
    nearest = (payload.get("nearest_area") or [None])[0] or {}
    condition = ((current.get("weatherDesc") or [{}])[0]).get("value", "Unknown")
    temp_c = _safe_int(current.get("temp_C"))
    feels_c = _safe_int(current.get("FeelsLikeC"))
    area_name = ((nearest.get("areaName") or [{}])[0]).get("value", location)

    summary_parts = [condition]
    if temp_c is not None:
        summary_parts.append(f"{temp_c}°C")
    if feels_c is not None and feels_c != temp_c:
        summary_parts.append(f"feels like {feels_c}°C")
    summary = ", ".join(summary_parts)

    return {
        "location": area_name,
        "condition": condition,
        "temperature_c": temp_c,
        "feels_like_c": feels_c,
        "summary": summary,
    }


async def handle_web_search(
    args: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Search the web for a short factual answer.

    Two-tier provider: Tavily when ``TAVILY_API_KEY`` is set (best
    quality, sourced summaries), DuckDuckGo Instant Answer otherwise
    (no key, narrow coverage — good for definitions, calculations,
    and simple facts but won't return news or rich results).

    Argument schema::

        {
          "query": "who won the latest world cup"
        }

    Result schema (success)::

        {
          "query":   "...",
          "answer":  "short factual summary suitable to read aloud",
          "source":  "https://example.com",
          "provider": "tavily" | "duckduckgo"
        }

    Result schema (failure)::

        {"error": "..."}
    """
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "Need a search query."}

    client = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    owns_client = http_client is None
    try:
        tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
        if tavily_key:
            result = await _tavily_search(client, query, tavily_key)
            if result is not None:
                return result
            # Fall through to DDG if Tavily failed.
        ddg_result = await _duckduckgo_search(client, query)
        return ddg_result
    finally:
        if owns_client:
            await client.aclose()


async def _tavily_search(
    client: httpx.AsyncClient,
    query: str,
    api_key: str,
) -> dict[str, Any] | None:
    """Tavily Search API — returns None on any failure so caller can
    fall back to DuckDuckGo. Doesn't raise."""
    try:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "include_answer": True,
                "max_results": 3,
            },
            headers={"User-Agent": _USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice.tools.web_search.tavily_failed", error=str(exc)[:200])
        return None

    answer = (payload.get("answer") or "").strip()
    results = payload.get("results") or []
    first = results[0] if results else {}
    if not answer and first:
        answer = (first.get("content") or "").strip()
    if not answer:
        return None
    # Trim very long answers — voice replies should stay short.
    answer = _truncate_for_speech(answer)
    return {
        "query": query,
        "answer": answer,
        "source": first.get("url") or "",
        "provider": "tavily",
    }


async def _duckduckgo_search(
    client: httpx.AsyncClient,
    query: str,
) -> dict[str, Any]:
    """DuckDuckGo Instant Answer fallback — free, narrow coverage."""
    try:
        response = await client.get(
            "https://api.duckduckgo.com/",
            params={
                "q": query,
                "format": "json",
                "no_html": "1",
                "skip_disambig": "1",
            },
            headers={"User-Agent": _USER_AGENT},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice.tools.web_search.ddg_failed", error=str(exc)[:200])
        return {
            "error": "Couldn't reach search.",
            "query": query,
            "provider": "duckduckgo",
        }

    abstract = (payload.get("AbstractText") or "").strip()
    answer = (payload.get("Answer") or "").strip()
    definition = (payload.get("Definition") or "").strip()
    source = (
        payload.get("AbstractURL") or payload.get("AnswerURL") or payload.get("DefinitionURL") or ""
    )
    text = abstract or answer or definition
    if not text:
        # DuckDuckGo IA gives nothing for many queries (especially news,
        # current events, anything time-sensitive). Be honest about that.
        return {
            "query": query,
            "answer": "",
            "provider": "duckduckgo",
            "note": (
                "DuckDuckGo Instant Answer returned no result for this "
                "query. Configure TAVILY_API_KEY for broader web search."
            ),
        }
    return {
        "query": query,
        "answer": _truncate_for_speech(text),
        "source": source,
        "provider": "duckduckgo",
    }


def _safe_zoneinfo(name: str) -> Any:
    """Return a ``zoneinfo.ZoneInfo`` if the name is valid, else UTC.

    Lazy import because ``zoneinfo`` requires tzdata on Alpine.
    """
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        return _dt.UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning("voice.tools.unknown_timezone", requested=name)
        return _dt.UTC


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _truncate_for_speech(text: str, *, max_chars: int = 320) -> str:
    """Trim search-result text to roughly two read-aloud sentences.

    LLM will further rephrase, but we don't want to feed it a 2000-char
    Wikipedia paragraph — that costs tokens and risks long replies.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rsplit(" ", 1)[0]
    return truncated.rstrip(",.;:") + "..."


# Public registry: function_name → handler. Used in
# ``register_voice_tools`` to short-circuit local tools so they don't
# pay the /voice/turn round-trip.
BUILTIN_TOOL_HANDLERS: dict[str, BuiltinToolHandler] = {
    "current_time": handle_current_time,
    "current_date": handle_current_date,
    "current_weather": handle_current_weather,
    "web_search": handle_web_search,
}
