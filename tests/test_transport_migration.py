from __future__ import annotations

import json
from typing import cast

import httpx
import pytest

from voice.internal_auth import validate_voice_service_jwt
from voice.pipeline import VoiceMetadata
from voice.transport import DailyApiClient, VoiceSessionManager


@pytest.mark.asyncio
async def test_daily_create_room_sets_geo_when_region_supplied(settings) -> None:
    seen_payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "name": "room-bom",
                "url": "https://orchet.daily.co/room-bom",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.daily.co/v1",
    ) as client:
        daily = DailyApiClient(settings, http_client=client)
        room = await daily.create_room("room-bom", 123456, geo_region="ap-south-1")

    assert room.url == "https://orchet.daily.co/room-bom"
    assert seen_payload["properties"] == {
        "exp": 123456,
        "eject_at_room_exp": True,
        "enable_prejoin_ui": False,
        "geo": "ap-south-1",
    }


@pytest.mark.asyncio
async def test_migrate_session_to_region_constructs_frame_and_spawns(settings) -> None:
    daily_requests: list[tuple[str, dict[str, object]]] = []
    internal_requests: list[httpx.Request] = []

    async def daily_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        daily_requests.append((request.url.path, payload))
        if request.url.path.endswith("/rooms"):
            return httpx.Response(
                200,
                json={
                    "name": payload["name"],
                    "url": f"https://orchet.daily.co/{payload['name']}",
                },
            )
        return httpx.Response(200, json={"token": f"token-{len(daily_requests)}"})

    async def internal_handler(request: httpx.Request) -> httpx.Response:
        internal_requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(daily_handler),
            base_url="https://api.daily.co/v1",
        ) as daily_http,
        httpx.AsyncClient(transport=httpx.MockTransport(internal_handler)) as internal_http,
    ):
        manager = VoiceSessionManager(
            settings,
            daily_client=DailyApiClient(settings, http_client=daily_http),
            internal_http_client=internal_http,
        )
        frame = await manager.migrate_session_to_region(
            "voice_test",
            "bom",
            metadata=VoiceMetadata(
                voice_session_id="voice_test",
                user_id="user_test",
                client_kind="web",
                region="iad",
                agent_id="orchet-super-agent",
            ),
            agent_manifest={"name": "test-agent"},
            locale_hint="hi-IN",
        )

    assert frame.type == "voice_session_migrate"
    assert frame.value.target_region == "bom"
    assert frame.value.new_room_url.startswith("https://orchet.daily.co/orchet-phase2-bom-")
    assert frame.value.new_client_token == "token-2"
    properties = cast(dict[str, object], daily_requests[0][1]["properties"])
    assert properties["geo"] == "ap-south-1"

    assert len(internal_requests) == 1
    request = internal_requests[0]
    assert request.url.host == "bom.orchet-voice.internal"
    body = json.loads(request.content.decode("utf-8"))
    assert body["session_id"] == "voice_test"
    assert body["room_url"] == frame.value.new_room_url
    assert body["locale_hint"] == "hi-IN"
    validate_voice_service_jwt(request.headers["authorization"], settings)
