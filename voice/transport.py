from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4

import httpx
import structlog
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.transports.services.daily import DailyParams, DailyTransport

from voice.auth import AuthenticatedUser
from voice.pipeline import EchoAudioProcessor, EchoMetadata
from voice.settings import Settings

logger = structlog.get_logger()


@dataclass(frozen=True)
class DailyRoom:
    name: str
    url: str


@dataclass(frozen=True)
class EchoSession:
    session_id: str
    room_name: str
    room_url: str
    client_token: str
    expires_at: int
    region: str


class DailyApiClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        if not settings.daily_api_key:
            raise ValueError("DAILY_API_KEY is required to create echo sessions")

        self._settings = settings
        self._http_client = http_client or httpx.AsyncClient(
            base_url="https://api.daily.co/v1",
            timeout=10.0,
            headers={
                "Authorization": f"Bearer {settings.daily_api_key}",
                "Content-Type": "application/json",
            },
        )
        self._owns_client = http_client is None

    async def create_room(self, name: str, expires_at: int) -> DailyRoom:
        response = await self._http_client.post(
            "/rooms",
            json={
                "name": name,
                "privacy": "private",
                "properties": {
                    "exp": expires_at,
                    "eject_at_room_exp": True,
                    "enable_prejoin_ui": False,
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        return DailyRoom(name=payload["name"], url=payload["url"])

    async def create_meeting_token(
        self,
        room_name: str,
        expires_at: int,
        *,
        is_owner: bool,
        user_name: str,
    ) -> str:
        response = await self._http_client.post(
            "/meeting-tokens",
            json={
                "properties": {
                    "room_name": room_name,
                    "exp": expires_at,
                    "is_owner": is_owner,
                    "user_name": user_name,
                }
            },
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("token")
        if not isinstance(token, str):
            raise RuntimeError("Daily did not return a meeting token")
        return token

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()


class EchoSessionManager:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._daily: DailyApiClient | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start_session(
        self,
        user: AuthenticatedUser,
        *,
        requested_session_id: str | None,
        client_kind: str,
        ttl_seconds: int,
    ) -> EchoSession:
        session_id = requested_session_id or f"voice_{uuid4().hex}"
        room_name = f"orchet-phase1-{uuid4().hex[:16]}"
        expires_at = int(time.time()) + ttl_seconds

        daily = self._daily_client()
        room = await daily.create_room(room_name, expires_at)
        bot_token = await daily.create_meeting_token(
            room.name,
            expires_at,
            is_owner=True,
            user_name="Orchet Echo Bot",
        )
        client_token = await daily.create_meeting_token(
            room.name,
            expires_at,
            is_owner=False,
            user_name=user.email or user.user_id,
        )

        task = asyncio.create_task(
            run_daily_echo_pipeline(
                room_url=room.url,
                bot_token=bot_token,
                daily_api_key=self._settings.daily_api_key,
                metadata=EchoMetadata(voice_session_id=session_id, client_kind=client_kind),
            ),
            name=f"daily-echo-{session_id}",
        )
        self._tasks[session_id] = task
        task.add_done_callback(lambda done: self._handle_done(session_id, done))

        logger.info(
            "voice.echo.session_started",
            voice_session_id=session_id,
            user_id=user.user_id,
            room_name=room.name,
            region=self._settings.region,
        )
        return EchoSession(
            session_id=session_id,
            room_name=room.name,
            room_url=room.url,
            client_token=client_token,
            expires_at=expires_at,
            region=self._settings.region,
        )

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        if self._daily:
            await self._daily.aclose()

    def _daily_client(self) -> DailyApiClient:
        if not self._daily:
            self._daily = DailyApiClient(self._settings)
        return self._daily

    def _handle_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(session_id, None)
        if task.cancelled():
            logger.info("voice.echo.session_cancelled", voice_session_id=session_id)
            return
        error = task.exception()
        if error:
            logger.error(
                "voice.echo.session_failed",
                voice_session_id=session_id,
                error=str(error),
            )


async def run_daily_echo_pipeline(
    *,
    room_url: str,
    bot_token: str,
    daily_api_key: str,
    metadata: EchoMetadata,
) -> None:
    transport = DailyTransport(
        room_url,
        bot_token,
        "Orchet Echo Bot",
        DailyParams(
            api_key=daily_api_key,
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=16000,
            audio_out_channels=1,
            transcription_enabled=False,
        ),
    )
    pipeline = Pipeline([transport.input(), EchoAudioProcessor(metadata), transport.output()])
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,
            audio_in_sample_rate=16000,
            audio_out_sample_rate=16000,
            enable_metrics=False,
            enable_usage_metrics=False,
        ),
        idle_timeout_secs=300,
    )
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
