from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from voice.settings import Settings


class AuthError(Exception):
    """Raised when a Supabase bearer token cannot be validated."""


class AuthConfigurationError(AuthError):
    """Raised when the validator is missing required Supabase configuration."""


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    email: str | None
    claims: dict[str, Any]


def extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("missing Authorization header")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization header must be Bearer <token>")
    return token.strip()


class SupabaseJwtValidator:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._http_client = http_client

    async def validate(self, token: str) -> AuthenticatedUser:
        if not self._settings.supabase_url or not self._settings.supabase_anon_key:
            raise AuthConfigurationError("Supabase URL and anon key are required")

        close_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=5.0)
        try:
            response = await client.get(
                f"{self._settings.supabase_url.rstrip('/')}/auth/v1/user",
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": self._settings.supabase_anon_key,
                },
            )
        finally:
            if close_client:
                await client.aclose()

        if response.status_code != 200:
            raise AuthError(f"Supabase rejected bearer token with {response.status_code}")

        payload = response.json()
        user_id = payload.get("id") or payload.get("user", {}).get("id")
        if not isinstance(user_id, str) or not user_id:
            raise AuthError("Supabase response did not include a user id")

        email = payload.get("email")
        return AuthenticatedUser(
            user_id=user_id,
            email=email if isinstance(email, str) else None,
            claims=payload,
        )


async def validate_authorization_header(
    authorization: str | None,
    settings: Settings,
    validator: SupabaseJwtValidator | None = None,
) -> AuthenticatedUser:
    token = extract_bearer_token(authorization)
    return await (validator or SupabaseJwtValidator(settings)).validate(token)
