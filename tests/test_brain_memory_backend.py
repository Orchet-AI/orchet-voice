"""Tests for voice.brain.memory_backend.BackendMemoryAdapter.

Locks in the fail-open contract — the adapter MUST return an empty
SessionContext on any failure mode (network error, HTTP 4xx/5xx, body
parse, timeout, missing user_id). The pipeline cannot react usefully
to a memory exception; the only acceptable behavior is "no context,
continue".

Happy path also tested for shape parity with the backend response.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from voice.brain.memory_backend import BackendMemoryAdapter
from voice.brain.memory_port import SessionContext


@pytest.fixture
def gateway_url() -> str:
    return "https://api.orchet.ai"


@pytest.fixture
def internal_token() -> str:
    return "test-token"


def _adapter_with_handler(
    gateway_url: str,
    internal_token: str,
    handler: httpx.MockTransport | None = None,
) -> BackendMemoryAdapter:
    """Build an adapter wired to an in-memory MockTransport so tests
    never hit a real network. The transport gets injected via a
    pre-constructed AsyncClient (Adapter owns is False)."""
    client = httpx.AsyncClient(
        base_url=gateway_url,
        transport=handler,
        timeout=2.0,
    )
    return BackendMemoryAdapter(
        gateway_url=gateway_url,
        internal_token=internal_token,
        http_client=client,
        timeout_s=2.0,
    )


@pytest.mark.asyncio
async def test_happy_path_decodes_backend_response(gateway_url: str, internal_token: str) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={
                "system_message": "USER CONTEXT\nProfile:\n- display_name: Kalas",
                "profile_loaded": True,
                "facts_count": 3,
                "elapsed_ms": 42,
            },
        )

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(
            user_id="u-1",
            voice_session_id="sess-1",
            agent_id="lumo-rentals",
            locale="en-IN",
        )
    finally:
        await adapter.aclose()

    assert ctx.system_message == "USER CONTEXT\nProfile:\n- display_name: Kalas"
    assert ctx.profile_loaded is True
    assert ctx.facts_count == 3
    assert ctx.elapsed_ms == 42
    assert ctx.partial is False
    assert ctx.has_content is True

    # Auth header forwarded; user_id present in body.
    assert "/voice/session-context" in captured["url"]
    assert captured["auth"] == f"Bearer {internal_token}"
    assert '"user_id": "u-1"' in captured["body"]
    assert '"voice_session_id": "sess-1"' in captured["body"]


@pytest.mark.asyncio
async def test_empty_user_id_returns_empty_context_without_request(
    gateway_url: str, internal_token: str
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="")
    finally:
        await adapter.aclose()

    assert called is False
    assert ctx.system_message is None
    assert ctx.has_content is False


@pytest.mark.asyncio
async def test_anon_user_returns_empty_context_without_request(
    gateway_url: str, internal_token: str
) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="anon")
    finally:
        await adapter.aclose()

    assert called is False
    assert ctx.system_message is None


@pytest.mark.asyncio
async def test_http_500_returns_empty_context_no_exception(
    gateway_url: str, internal_token: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    assert ctx == SessionContext(
        system_message=None,
        profile_loaded=False,
        facts_count=0,
        elapsed_ms=0,
        partial=False,
    )


@pytest.mark.asyncio
async def test_http_400_returns_empty_context_no_exception(
    gateway_url: str, internal_token: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"system_message": None, "facts_count": 0})

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    assert ctx.has_content is False


@pytest.mark.asyncio
async def test_network_error_returns_empty_context_no_exception(
    gateway_url: str, internal_token: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    assert ctx.has_content is False
    assert ctx.facts_count == 0


@pytest.mark.asyncio
async def test_timeout_returns_empty_context_no_exception(
    gateway_url: str, internal_token: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1.0)
        return httpx.Response(200, json={"facts_count": 0})

    client = httpx.AsyncClient(
        base_url=gateway_url,
        transport=httpx.MockTransport(handler),
        timeout=10.0,
    )
    adapter = BackendMemoryAdapter(
        gateway_url=gateway_url,
        internal_token=internal_token,
        http_client=client,
        timeout_s=0.05,
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    assert ctx.has_content is False


@pytest.mark.asyncio
async def test_malformed_json_returns_empty_context_no_exception(
    gateway_url: str, internal_token: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    assert ctx.has_content is False


@pytest.mark.asyncio
async def test_partial_flag_propagates(gateway_url: str, internal_token: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "system_message": "USER CONTEXT\nProfile:\n- timezone: UTC",
                "profile_loaded": True,
                "facts_count": 0,
                "elapsed_ms": 410,
                "partial": True,
            },
        )

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    assert ctx.partial is True
    assert ctx.profile_loaded is True
    assert ctx.has_content is True


@pytest.mark.asyncio
async def test_string_with_only_whitespace_treated_as_empty(
    gateway_url: str, internal_token: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "system_message": "   \n  \t  ",
                "profile_loaded": False,
                "facts_count": 0,
                "elapsed_ms": 5,
            },
        )

    adapter = _adapter_with_handler(
        gateway_url, internal_token, handler=httpx.MockTransport(handler)
    )
    try:
        ctx = await adapter.get_session_context(user_id="u-1")
    finally:
        await adapter.aclose()

    # The adapter still surfaces the raw string — the has_content
    # property is what gates the prompt-append. Whitespace-only must
    # not produce a system-prompt injection.
    assert ctx.has_content is False
