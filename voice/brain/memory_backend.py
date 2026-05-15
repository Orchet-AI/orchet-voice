"""HTTP adapter for MemoryPort — calls orchet-brain POST /memory/session-context.

Until orchet-brain merge #1, this adapter used to call the orchestrator
gateway at /voice/session-context (transitional shim). After the
rehome it now goes direct to the Modal-hosted brain at
ORCHET_ML_BRAIN_URL — same wire shape, different URL + different
auth scheme. The transitional gateway route in orchet-backend will be
deleted in a follow-up once this path is verified end-to-end.

Auth:
    Brain validates JWT signed with HS256 + LUMO_ML_SERVICE_JWT_SECRET,
    with issuer=lumo-core, audience=lumo-ml. We mint a fresh per-call
    JWT here (60-second TTL) so every request carries a unique jti and
    a non-anon sub. Brain rejects sub="anon" outright.

Fail-open contract — every layer:
    - Network error → empty SessionContext, log + carry on.
    - HTTP 4xx / 5xx → empty SessionContext, log + carry on.
    - Body parse error → empty SessionContext, log + carry on.
    - Wall-clock budget exceeded → empty SessionContext, log + carry on.
    - Missing config (brain URL or JWT secret) → empty SessionContext,
      no HTTP issued, log once.

The pipeline cannot usefully react to a memory exception — a session
that knows less about the user is always preferable to one that won't
start. Every error path here is silent except for a single structured
log line.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from uuid import uuid4

import httpx
import jwt
import structlog

from voice.brain.memory_port import MemoryPort, SessionContext

logger = structlog.get_logger()

# Wall-clock budget for the call. Pairs with the brain's 400ms server-
# side compose budget — the extra 100ms covers network + JSON parsing +
# httpx overhead. Modal cold-starts can blow past this; the next call
# after a cold start will land within budget.
DEFAULT_FETCH_TIMEOUT_S = 0.5

# Brain's require_lumo_jwt expects these exact claim values.
_BRAIN_JWT_ISSUER = "lumo-core"
_BRAIN_JWT_AUDIENCE = "lumo-ml"
_BRAIN_JWT_SCOPE = "voice.memory.session_context"
_BRAIN_JWT_TTL_SECONDS = 60


class BrainMemoryAdapter:
    """MemoryPort adapter against orchet-brain POST /memory/session-context.

    Holds a persistent httpx.AsyncClient bound to the brain Modal URL.
    Lifecycle managed by the caller; close via ``aclose()``.
    """

    def __init__(
        self,
        *,
        brain_url: str,
        jwt_secret: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_FETCH_TIMEOUT_S,
    ) -> None:
        self._brain_url = brain_url.rstrip("/")
        self._jwt_secret = jwt_secret
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
        if not self._brain_url or not self._jwt_secret:
            logger.debug(
                "voice.brain.memory.not_configured",
                user_id_redacted=_hash_user(user_id),
                brain_url_set=bool(self._brain_url),
                jwt_secret_set=bool(self._jwt_secret),
            )
            return _EMPTY_CONTEXT

        payload: dict[str, Any] = {"user_id": user_id}
        if voice_session_id:
            payload["voice_session_id"] = voice_session_id
        if agent_id:
            payload["agent_id"] = agent_id
        if locale:
            payload["locale"] = locale

        try:
            token = _mint_brain_jwt(self._jwt_secret, subject=user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "voice.brain.memory.jwt_sign_failed",
                user_id_redacted=_hash_user(user_id),
                error=str(exc)[:200],
            )
            return _EMPTY_CONTEXT

        try:
            response = await asyncio.wait_for(
                self._client().post(
                    "/memory/session-context",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
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
            self._http_client = httpx.AsyncClient(
                base_url=self._brain_url,
                timeout=httpx.Timeout(
                    connect=2.0,
                    read=max(self._timeout_s + 0.5, 1.0),
                    write=2.0,
                    pool=2.0,
                ),
            )
        return self._http_client


def create_brain_memory_adapter(
    *,
    brain_url: str,
    jwt_secret: str,
) -> MemoryPort:
    """Factory — returns the HTTP adapter typed as MemoryPort.

    Callers should depend on the Protocol, not the concrete class, so
    a fake can be swapped in without import gymnastics.
    """
    return BrainMemoryAdapter(brain_url=brain_url, jwt_secret=jwt_secret)


def _mint_brain_jwt(secret: str, *, subject: str) -> str:
    """Mint a short-lived service JWT brain will accept.

    Claims required by orchet-brain/lumo_ml/auth.py:
        iss = "lumo-core"
        aud = "lumo-ml"
        sub = non-empty, non-"anon"
        jti = present (request id)
        scope = present
        exp = future
    """
    now = int(time.time())
    payload = {
        "iss": _BRAIN_JWT_ISSUER,
        "aud": _BRAIN_JWT_AUDIENCE,
        "sub": subject,
        "jti": uuid4().hex,
        "scope": _BRAIN_JWT_SCOPE,
        "iat": now,
        "exp": now + _BRAIN_JWT_TTL_SECONDS,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


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
