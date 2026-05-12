from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4

from voice.auth import AuthError, extract_bearer_token
from voice.settings import Settings

SERVICE_JWT_AUDIENCE = "orchet-voice-internal"
SERVICE_JWT_ISSUER = "orchet-voice"
SERVICE_JWT_SCOPE = "voice.session.spawn"


def sign_voice_service_jwt(
    settings: Settings,
    *,
    subject: str,
    scope: str = SERVICE_JWT_SCOPE,
    audience: str = SERVICE_JWT_AUDIENCE,
    ttl_seconds: int = 60,
) -> str:
    if not settings.internal_token:
        raise AuthError("ORCHET_INTERNAL_TOKEN is required for voice service JWT signing")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": SERVICE_JWT_ISSUER,
        "aud": audience,
        "sub": subject,
        "jti": uuid4().hex,
        "scope": scope,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    encoded_header = _base64url_json(header)
    encoded_payload = _base64url_json(payload)
    signature = _signature(settings.internal_token, f"{encoded_header}.{encoded_payload}")
    return f"{encoded_header}.{encoded_payload}.{signature}"


def validate_voice_service_jwt(
    authorization: str | None,
    settings: Settings,
    *,
    scope: str = SERVICE_JWT_SCOPE,
    audience: str = SERVICE_JWT_AUDIENCE,
) -> dict[str, object]:
    if not settings.internal_token:
        raise AuthError("ORCHET_INTERNAL_TOKEN is required for voice service JWT validation")
    token = extract_bearer_token(authorization)
    try:
        encoded_header, encoded_payload, received_signature = token.split(".", maxsplit=2)
    except ValueError as exc:
        raise AuthError("service JWT must have three segments") from exc

    expected_signature = _signature(settings.internal_token, f"{encoded_header}.{encoded_payload}")
    if not hmac.compare_digest(received_signature, expected_signature):
        raise AuthError("service JWT signature mismatch")

    header = _base64url_json_decode(encoded_header)
    payload = _base64url_json_decode(encoded_payload)
    if header.get("alg") != "HS256":
        raise AuthError("service JWT must use HS256")
    now = int(time.time())
    if payload.get("iss") != SERVICE_JWT_ISSUER:
        raise AuthError("service JWT issuer mismatch")
    if payload.get("aud") != audience:
        raise AuthError("service JWT audience mismatch")
    if payload.get("scope") != scope:
        raise AuthError("service JWT scope mismatch")
    if not isinstance(payload.get("sub"), str) or not payload["sub"]:
        raise AuthError("service JWT subject is required")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < now:
        raise AuthError("service JWT expired")
    return payload


def _signature(secret: str, signing_input: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return _base64url_bytes(digest)


def _base64url_json(value: object) -> str:
    return _base64url_bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _base64url_json_decode(value: str) -> dict[str, object]:
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    payload = json.loads(decoded)
    if not isinstance(payload, dict):
        raise AuthError("service JWT segment must be a JSON object")
    return payload


def _base64url_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
