from __future__ import annotations

import httpx
import pytest

from voice.auth import AuthError, SupabaseJwtValidator, extract_bearer_token
from voice.settings import Settings


def test_extract_bearer_token() -> None:
    assert extract_bearer_token("Bearer abc.def") == "abc.def"


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer "])
def test_extract_bearer_token_rejects_invalid_header(header: str | None) -> None:
    with pytest.raises(AuthError):
        extract_bearer_token(header)


async def test_supabase_validator_accepts_valid_token(settings: Settings) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer valid-token"
        assert request.headers["apikey"] == "test-anon"
        return httpx.Response(200, json={"id": "user_123", "email": "user@example.com"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        user = await SupabaseJwtValidator(settings, client).validate("valid-token")

    assert user.user_id == "user_123"
    assert user.email == "user@example.com"


async def test_supabase_validator_rejects_invalid_token(settings: Settings) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"msg": "invalid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AuthError):
            await SupabaseJwtValidator(settings, client).validate("bad-token")
