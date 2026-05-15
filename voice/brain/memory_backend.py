"""HTTP adapter for MemoryPort — calls orchet-backend POST /voice/session-context.

This is the only adapter for MemoryPort today. It calls the same
endpoint family the rest of voice already uses (gateway_url +
internal_token bearer), so the auth + routing path is proven.

Fail-open contract:
    - Network error → empty SessionContext, log + carry on.
    - HTTP 4xx / 5xx → empty SessionContext, log + carry on.
    - Body parse error → empty SessionContext, log + carry on.
    - Wall-clock budget exceeded → empty SessionContext, log + carry on.

The pipeline cannot usefully react to a memory exception — a session
that knows less about the user is always preferable to one that won't
start. Every error path here is silent except for a single structured
log line.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from voice.brain.memory_port import MemoryPort, SessionContext

logger = structlog.get_logger()

# Wall-clock budget for the call. Pairs with the backend's 400ms
# server-side compose budget — the extra 100ms covers network +
# JSON parsing + the voice service's own httpx overhead.
DEFAULT_FETCH_TIMEOUT_S = 0.5


class BackendMemoryAdapter:
    """MemoryPort adapter against orchet-backend /voice/session-context.

    Holds a persistent httpx.AsyncClient for low-overhead repeat calls
    on the same voice session manager. Lifecycle managed by the
    enclosing VoiceSessionManager — call ``aclose()`` on shutdown.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        internal_token: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    ) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._internal_token = internal_token
        self._timeout_s = timeout_s
        self._http_client = http_client
        self._owns_client = http_client is None

    async def get_session_context(
        self,
        *,
        user_id: str,
        voice_session_id: str | None = None,
        agent_id: str | None = None,
        locale: str | None = None,
    ) -> SessionContext:
        if not user_id or user_id == "anon":
            return _EMPTY_CONTEXT

        payload: dict[str, Any] = {"user_id": user_id}
        if voice_session_id:
            payload["voice_session_id"] = voice_session_id
        if agent_id:
            payload["agent_id"] = agent_id
        if locale:
            payload["locale"] = locale

        try:
            response = await asyncio.wait_for(
                self._client().post(
                    "/voice/session-context",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self._internal_token}",
                    },
                ),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "voice.brain.memory.fetch_timeout",
                user_id_redacted=_hash_user(user_id),
                voice_session_id=voice_session_id,
                timeout_s=self._timeout_s,
            )
            return _EMPTY_CONTEXT
        except httpx.HTTPError as exc:
            logger.warning(
                "voice.brain.memory.fetch_failed",
                user_id_redacted=_hash_user(user_id),
                voice_session_id=voice_session_id,
                error=str(exc)[:200],
            )
            return _EMPTY_CONTEXT

        if response.status_code != 200:
            logger.warning(
                "voice.brain.memory.fetch_status",
                user_id_redacted=_hash_user(user_id),
                voice_session_id=voice_session_id,
                status=response.status_code,
            )
            return _EMPTY_CONTEXT

        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice.brain.memory.parse_failed",
                user_id_redacted=_hash_user(user_id),
                voice_session_id=voice_session_id,
                error=str(exc)[:200],
            )
            return _EMPTY_CONTEXT

        return _decode_response(body)

    async def aclose(self) -> None:
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _client(self) -> httpx.AsyncClient:
        if not self._http_client:
            # Aggressive split timeouts: connect should be fast since
            # the gateway is colocated; read is capped by the outer
            # wait_for above but we still set a generous read so
            # individual request timing failures surface as
            # asyncio.TimeoutError (more predictable than httpx's own).
            self._http_client = httpx.AsyncClient(
                base_url=self._gateway_url,
                timeout=httpx.Timeout(
                    connect=2.0,
                    read=max(self._timeout_s + 0.5, 1.0),
                    write=2.0,
                    pool=2.0,
                ),
            )
        return self._http_client


def create_backend_memory_adapter(
    *,
    gateway_url: str,
    internal_token: str,
) -> MemoryPort:
    """Factory — returns the HTTP adapter typed as MemoryPort.

    Consumers should depend on the Protocol, not the concrete class,
    so the test fake can be swapped in without import gymnastics.
    """
    return BackendMemoryAdapter(
        gateway_url=gateway_url,
        internal_token=internal_token,
    )


def _decode_response(body: Any) -> SessionContext:
    if not isinstance(body, dict):
        return _EMPTY_CONTEXT
    raw_message = body.get("system_message")
    system_message = raw_message if isinstance(raw_message, str) else None
    profile_loaded = bool(body.get("profile_loaded"))
    facts_count = _as_int(body.get("facts_count"))
    elapsed_ms = _as_int(body.get("elapsed_ms"))
    partial = bool(body.get("partial"))
    return SessionContext(
        system_message=system_message,
        profile_loaded=profile_loaded,
        facts_count=facts_count,
        elapsed_ms=elapsed_ms,
        partial=partial,
    )


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _hash_user(user_id: str) -> str:
    """Short hash for structured logs — not security-grade. Real PII
    redaction is the responsibility of the log ingestion pipeline."""
    h = 0
    for ch in user_id:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return format(h, "x")


_EMPTY_CONTEXT = SessionContext(
    system_message=None,
    profile_loaded=False,
    facts_count=0,
    elapsed_ms=0,
    partial=False,
)
