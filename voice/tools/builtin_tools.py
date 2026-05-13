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


BuiltinToolHandler = Callable[..., Awaitable[dict[str, Any]]]


# Per-call execution context passed to handlers that need it (e.g.
# marketplace install needs the user's identity and a service-JWT-
# signed call to orchet-backend). Plain handlers like current_time
# ignore it via **kwargs.
class BuiltinToolContext(dict[str, Any]):
    """Minimal duck-typed context: keys are ``user_id``, ``session_id``,
    ``gateway_url``, ``internal_token``. None of the existing handlers
    use it; new backend-aware handlers read it via ``ctx['user_id']``.
    """


async def handle_current_time(args: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
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


async def handle_current_date(args: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
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
    **_kwargs: Any,
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
    **_kwargs: Any,
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


# ---------------------------------------------------------------------------
# Marketplace discovery + voice-driven install
# ---------------------------------------------------------------------------

# Coarse domain → category map used to filter the marketplace catalog
# when Haiku passes a free-text task description. We keep it short
# and forgiving — if a keyword matches anywhere in the user's task
# description OR in an agent's manifest.domain / category / intents
# we count it as a hit.
_MARKETPLACE_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "flights": ("flight", "fly", "airline", "ticket"),
    "hotels": ("hotel", "stay", "room", "accommodation"),
    "food": ("food", "restaurant", "order", "delivery", "eat", "pizza", "meal"),
    "restaurant": ("reservation", "book a table", "dine"),
    "weather": ("weather", "rain", "forecast", "temperature"),
    "maps": ("map", "directions", "route", "navigate"),
    "ev-charging": ("ev", "charger", "charging", "electric"),
    "events": ("event", "concert", "show"),
    "attractions": ("attraction", "museum", "tourist", "sightseeing"),
    "tours": ("tour", "experience", "excursion"),
}


def _agents_match_task(
    agents: list[dict[str, Any]],
    task_description: str,
) -> list[dict[str, Any]]:
    """Filter the marketplace catalog to agents likely relevant to the
    user's task. Soft match: matches if any keyword for the agent's
    domain/category appears in the task description OR vice-versa.
    Empty match returns ALL agents (let Haiku narrow) rather than
    dropping the whole list silently.
    """
    needle = task_description.lower()
    matched: list[dict[str, Any]] = []
    for agent in agents:
        if agent.get("source") == "coming_soon":
            continue
        domain = (agent.get("domain") or "").lower()
        category = ((agent.get("listing") or {}).get("category") or "").lower()
        intents = " ".join(agent.get("intents") or []).lower()
        haystack = f"{domain} {category} {intents}"
        keywords = _MARKETPLACE_INTENT_KEYWORDS.get(domain, ())
        if (
            any(k in needle for k in keywords)
            or any(k in haystack for k in needle.split())
            or domain in needle
            or category in needle
        ):
            matched.append(agent)
    return matched or agents


def _rank_for_user(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank ranked-already agents: prefer installed > higher install_count > rating."""

    def key(a: dict[str, Any]) -> tuple[int, int, float]:
        installed = (a.get("install") or {}).get("status") == "installed"
        return (
            1 if installed else 0,
            int(a.get("install_count") or 0),
            float(a.get("rating_avg") or 0.0),
        )

    return sorted(agents, key=key, reverse=True)


async def handle_marketplace_find_agents(
    args: dict[str, Any],
    *,
    ctx: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Discover marketplace agents matching the user's task.

    Calls ``GET /marketplace`` on orchet-backend, narrows by the
    user's task description, ranks (installed first, then by
    install_count, then rating), and returns the top three. Haiku
    reads the names out loud and asks the user to pick.
    """
    task_description = (args.get("task_description") or "").strip()
    if not task_description:
        return {"error": "Need a task description to search the marketplace."}
    if ctx is None:
        return {"error": "marketplace_find_agents needs voice session context."}

    gateway_url = (ctx.get("gateway_url") or "").rstrip("/")
    if not gateway_url:
        return {"error": "marketplace lookup is not configured."}
    headers: dict[str, str] = {"User-Agent": _USER_AGENT}
    user_id = ctx.get("user_id")
    if user_id:
        headers["x-orchet-user-id"] = user_id
    internal_token = ctx.get("internal_token")
    if internal_token:
        headers["Authorization"] = f"Bearer {internal_token}"

    client = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    owns_client = http_client is None
    try:
        response = await client.get(f"{gateway_url}/marketplace", headers=headers)
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        logger.warning("voice.tools.marketplace_find.timeout")
        return {"error": "Marketplace didn't respond in time."}
    except Exception as exc:  # noqa: BLE001
        logger.warning("voice.tools.marketplace_find.failed", error=str(exc)[:200])
        return {"error": "Couldn't reach the marketplace."}
    finally:
        if owns_client:
            await client.aclose()

    agents = payload.get("agents") or []
    if not isinstance(agents, list):
        return {"error": "Marketplace returned an unexpected shape."}

    matched = _agents_match_task(agents, task_description)
    ranked = _rank_for_user(matched)[:3]
    result_agents = [
        {
            "agent_id": a.get("agent_id"),
            "display_name": a.get("display_name"),
            "one_liner": a.get("one_liner"),
            "domain": a.get("domain"),
            "rating_avg": a.get("rating_avg"),
            "install_count": a.get("install_count"),
            "installed": (a.get("install") or {}).get("status") == "installed",
        }
        for a in ranked
    ]
    return {"task_description": task_description, "agents": result_agents}


async def handle_marketplace_install_agent(
    args: dict[str, Any],
    *,
    ctx: dict[str, Any] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Install an agent the user has confirmed. Calls
    ``POST /voice/marketplace/install`` on orchet-backend which
    auto-derives consent + grants manifest-declared scopes.
    """
    agent_id = (args.get("agent_id") or "").strip()
    if not agent_id:
        return {"error": "Need an agent_id to install."}
    if ctx is None:
        return {"error": "marketplace_install_agent needs voice session context."}

    gateway_url = (ctx.get("gateway_url") or "").rstrip("/")
    user_id = ctx.get("user_id")
    if not gateway_url or not user_id:
        return {"error": "marketplace install is not configured."}
    headers: dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "x-orchet-user-id": user_id,
        "Content-Type": "application/json",
    }
    internal_token = ctx.get("internal_token")
    if internal_token:
        headers["Authorization"] = f"Bearer {internal_token}"

    client = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)
    owns_client = http_client is None
    try:
        response = await client.post(
            f"{gateway_url}/voice/marketplace/install",
            headers=headers,
            json={"agent_id": agent_id},
        )
        if response.status_code >= 400:
            try:
                err = response.json()
            except Exception:  # noqa: BLE001
                err = {"error": response.text[:160]}
            logger.warning(
                "voice.tools.marketplace_install.http_error",
                status=response.status_code,
                body=str(err)[:200],
            )
            return {
                "error": err.get("error", "install_failed"),
                "agent_id": agent_id,
            }
        return response.json()
    except httpx.TimeoutException:
        logger.warning("voice.tools.marketplace_install.timeout", agent_id=agent_id)
        return {"error": "Install didn't respond in time."}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "voice.tools.marketplace_install.failed",
            agent_id=agent_id,
            error=str(exc)[:200],
        )
        return {"error": "Couldn't reach the install endpoint."}
    finally:
        if owns_client:
            await client.aclose()


# Public registry: function_name → handler. Used in
# ``register_voice_tools`` to short-circuit local tools so they don't
# pay the /voice/turn round-trip.
BUILTIN_TOOL_HANDLERS: dict[str, BuiltinToolHandler] = {
    "current_time": handle_current_time,
    "current_date": handle_current_date,
    "current_weather": handle_current_weather,
    "web_search": handle_web_search,
    "marketplace_find_agents": handle_marketplace_find_agents,
    "marketplace_install_agent": handle_marketplace_install_agent,
}
