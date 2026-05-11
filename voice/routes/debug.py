from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from voice.auth import AuthConfigurationError, AuthError, validate_authorization_header
from voice.settings import Settings
from voice.transport import EchoSession, EchoSessionManager

router = APIRouter()


class EchoConnectRequest(BaseModel):
    voice_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_kind: Literal["web", "ios"] = "web"
    ttl_seconds: int = Field(default=600, ge=60, le=1800)


class EchoConnectResponse(BaseModel):
    voice_session_id: str
    room_name: str
    room_url: str
    client_token: str
    expires_at: int
    region: str


@router.post("/debug/echo", response_model=EchoConnectResponse)
async def create_debug_echo_session(
    payload: EchoConnectRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> EchoConnectResponse:
    settings: Settings = request.app.state.settings
    sessions: EchoSessionManager = request.app.state.echo_sessions

    try:
        user = await validate_authorization_header(authorization, settings)
    except AuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    try:
        session = await sessions.start_session(
            user,
            requested_session_id=payload.voice_session_id,
            client_kind=payload.client_kind,
            ttl_seconds=payload.ttl_seconds,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to create Daily echo session: {exc}",
        ) from exc

    return _to_response(session)


def _to_response(session: EchoSession) -> EchoConnectResponse:
    return EchoConnectResponse(
        voice_session_id=session.session_id,
        room_name=session.room_name,
        room_url=session.room_url,
        client_token=session.client_token,
        expires_at=session.expires_at,
        region=session.region,
    )
