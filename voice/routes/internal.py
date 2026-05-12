from __future__ import annotations

from ipaddress import ip_address
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from voice.auth import AuthError
from voice.internal_auth import validate_voice_service_jwt
from voice.settings import Settings
from voice.transport import VoiceSessionManager

router = APIRouter()


class InternalSpawnSessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    client_kind: Literal["web", "ios"] = "web"
    room_name: str = Field(min_length=1, max_length=128)
    room_url: str = Field(min_length=1)
    expires_at: int
    locale_hint: str = Field(default="unknown", min_length=1, max_length=16)
    agent_id: str = Field(default="orchet-super-agent", min_length=1, max_length=128)
    agent_manifest: dict[str, Any] | None = None


@router.post("/internal/spawn_session")
async def spawn_internal_session(
    payload: InternalSpawnSessionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    fly_client_ip: str | None = Header(default=None, alias="Fly-Client-IP"),
) -> dict[str, bool]:
    # This route shares the public FastAPI listener to avoid Fly service config
    # churn, but it is gated to same-app Fly 6PN callers plus a service JWT.
    if not _is_fly_6pn_client(fly_client_ip):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "fly_6pn_required"},
        )

    settings: Settings = request.app.state.settings
    try:
        validate_voice_service_jwt(authorization, settings)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_service_bearer", "message": str(exc)},
        ) from exc

    sessions: VoiceSessionManager = request.app.state.voice_sessions
    await sessions.spawn_internal_session(
        session_id=payload.session_id,
        user_id=payload.user_id,
        client_kind=payload.client_kind,
        room_name=payload.room_name,
        room_url=payload.room_url,
        expires_at=payload.expires_at,
        locale_hint=payload.locale_hint,
        agent_id=payload.agent_id,
        agent_manifest=payload.agent_manifest,
    )
    return {"ok": True}


def _is_fly_6pn_client(value: str | None) -> bool:
    if not value:
        return False
    candidate = value.strip()
    try:
        parsed = ip_address(candidate)
    except ValueError:
        return False
    return parsed.version == 6 and parsed.compressed.startswith("fdaa:")
