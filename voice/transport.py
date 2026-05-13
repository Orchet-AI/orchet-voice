from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
import structlog
from deepgram import LiveOptions
from pipecat.frames.frames import (
    Frame,
    FunctionCallResultProperties,
    LLMFullResponseEndFrame,
    TransportMessageUrgentFrame,
    TTSTextFrame,
)
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram import DeepgramSTTService, DeepgramTTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport

from voice.auth import AuthenticatedUser
from voice.internal_auth import sign_voice_service_jwt
from voice.obs.cost import VoiceSessionCostTracker, set_cost_span_attributes
from voice.obs.tracing import get_tracer
from voice.pipeline import (
    AudioDurationCostProcessor,
    ClientVADInterruptionProcessor,
    LLMSpanProcessor,
    STTSpanProcessor,
    TTSSpanProcessor,
    VoiceMetadata,
    VoiceTurnTracker,
)
from voice.protocol.migration import VoiceSessionMigrate, VoiceSessionMigrateValue
from voice.providers.stt_sarvam import SarvamSTTService
from voice.providers.tts_deepgram_ws import DeepgramStreamingTTSService
from voice.providers.tts_sarvam import SarvamTTSService
from voice.routing.language_router import (
    LanguageDetectionProcessor,
    LanguagePromptProcessor,
    ProviderGateProcessor,
    SarvamStreamingLanguageDetector,
    load_voice_prompt,
    sarvam_locale_for,
    stt_gate_types,
    tts_gate_types,
)
from voice.routing.llm_router import (
    agent_manifest_for,
    llm_model_for,
    llm_provider_for,
    pick_llm_service,
)
from voice.routing.region_router import (
    SARVAM_PREFERRED_REGIONS,
    pick_target_region,
    should_migrate_for_sarvam,
)
from voice.settings import Settings
from voice.tool_catalog import VOICE_FUNCTION_SCHEMAS, VOICE_TOOLS_SCHEMA
from voice.voice_turn_dispatcher import VoiceTurnDispatcher

logger = structlog.get_logger()

MIGRATION_VALID_FOR_SECONDS = 120
OLD_SESSION_GRACE_SECONDS = 10
INTERNAL_APP_NAME = "orchet-voice"


@dataclass(frozen=True)
class DailyRoom:
    name: str
    url: str


@dataclass(frozen=True)
class VoiceSession:
    session_id: str
    room_name: str
    room_url: str
    client_token: str
    expires_at: int
    region: str


class DailyApiClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        if not settings.daily_api_key:
            raise ValueError("DAILY_API_KEY is required to create voice sessions")

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

    async def create_room(
        self,
        name: str,
        expires_at: int,
        *,
        geo_region: str | None = None,
    ) -> DailyRoom:
        properties: dict[str, Any] = {
            "exp": expires_at,
            "eject_at_room_exp": True,
            "enable_prejoin_ui": False,
        }
        if geo_region:
            # Daily's room REST API calls this property `geo` and uses AWS-style
            # region ids (for example ap-south-1 for Mumbai), not Fly region ids.
            properties["geo"] = geo_region
        response = await self._http_client.post(
            "/rooms",
            json={
                "name": name,
                "privacy": "private",
                "properties": properties,
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


class VoiceSessionManager:
    def __init__(
        self,
        settings: Settings,
        *,
        daily_client: DailyApiClient | None = None,
        internal_http_client: httpx.AsyncClient | None = None,
    ):
        self._settings = settings
        self._daily: DailyApiClient | None = daily_client
        self._owns_internal_client = internal_http_client is None
        self._internal_http_client = internal_http_client or httpx.AsyncClient(timeout=10.0)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start_session(
        self,
        user: AuthenticatedUser,
        *,
        requested_session_id: str | None,
        client_kind: str,
        ttl_seconds: int,
        agent_id: str = "orchet-super-agent",
        agent_manifest: dict[str, Any] | None = None,
    ) -> VoiceSession:
        self._validate_provider_settings()

        session_id = requested_session_id or f"voice_{uuid4().hex}"
        room_name = f"orchet-phase2-{uuid4().hex[:16]}"
        expires_at = int(time.time()) + ttl_seconds

        daily = self._daily_client()
        room = await daily.create_room(room_name, expires_at)
        bot_token = await daily.create_meeting_token(
            room.name,
            expires_at,
            is_owner=True,
            user_name="Orchet Voice Bot",
        )
        client_token = await daily.create_meeting_token(
            room.name,
            expires_at,
            is_owner=False,
            user_name=user.email or user.user_id,
        )

        metadata = VoiceMetadata(
            voice_session_id=session_id,
            user_id=user.user_id,
            client_kind=client_kind,
            region=self._settings.region,
            agent_id=agent_id,
            llm_model=self._settings.voice_llm_model,
            tts_voice_id=self._settings.voice_tts_voice,
        )
        self._spawn_pipeline(
            session_id=session_id,
            room_url=room.url,
            bot_token=bot_token,
            metadata=metadata,
            agent_manifest=agent_manifest,
        )

        logger.info(
            "voice.session_started",
            voice_session_id=session_id,
            user_id=user.user_id,
            room_name=room.name,
            region=self._settings.region,
        )
        return VoiceSession(
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
        if self._owns_internal_client:
            await self._internal_http_client.aclose()

    def _daily_client(self) -> DailyApiClient:
        if not self._daily:
            self._daily = DailyApiClient(self._settings)
        return self._daily

    def _handle_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(session_id, None)
        if task.cancelled():
            logger.info("voice.session_cancelled", voice_session_id=session_id)
            return
        error = task.exception()
        if error:
            logger.error(
                "voice.session_failed",
                voice_session_id=session_id,
                error=str(error),
            )

    async def migrate_session_to_region(
        self,
        session_id: str,
        target_region: str,
        *,
        metadata: VoiceMetadata,
        agent_manifest: dict[str, Any] | None,
        locale_hint: str,
    ) -> VoiceSessionMigrate:
        """Prepare a target-region worker and return the client reconnect frame.

        Option A from the PR brief is intentional here: Daily room `geo` alone
        moves the SFU, but not the Fly worker that streams audio to Sarvam.
        The internal spawn request starts the bot on the target Fly machine so
        the Daily-to-Fly-to-Sarvam path also moves close to India.
        """
        target = target_region.strip().lower()
        if target not in SARVAM_PREFERRED_REGIONS:
            raise ValueError(f"unsupported Sarvam migration region: {target_region}")

        expires_at = int(time.time()) + MIGRATION_VALID_FOR_SECONDS
        room_name = f"orchet-phase2-{target}-{uuid4().hex[:12]}"
        daily = self._daily_client()
        room = await daily.create_room(
            room_name,
            expires_at,
            geo_region=daily_geo_region_for_fly_region(target),
        )
        client_token = await daily.create_meeting_token(
            room.name,
            expires_at,
            is_owner=False,
            user_name=metadata.user_id,
        )
        await self._spawn_session_on_region(
            target_region=target,
            payload={
                "session_id": session_id,
                "user_id": metadata.user_id,
                "client_kind": metadata.client_kind,
                "room_name": room.name,
                "room_url": room.url,
                "expires_at": expires_at,
                "locale_hint": locale_hint,
                "agent_id": metadata.agent_id,
                "agent_manifest": agent_manifest,
            },
            subject=metadata.user_id,
        )
        return VoiceSessionMigrate(
            value=VoiceSessionMigrateValue(
                target_region=cast(Literal["bom", "sin"], target),
                new_room_url=room.url,
                new_client_token=client_token,
                valid_for_seconds=MIGRATION_VALID_FOR_SECONDS,
                preserve_session_id=True,
            )
        )

    async def spawn_internal_session(
        self,
        *,
        session_id: str,
        user_id: str,
        client_kind: str,
        room_name: str,
        room_url: str,
        expires_at: int,
        locale_hint: str,
        agent_id: str,
        agent_manifest: dict[str, Any] | None = None,
    ) -> None:
        self._validate_provider_settings()
        bot_token = await self._daily_client().create_meeting_token(
            room_name,
            expires_at,
            is_owner=True,
            user_name="Orchet Voice Bot",
        )
        metadata = VoiceMetadata(
            voice_session_id=session_id,
            user_id=user_id,
            client_kind=client_kind,
            region=self._settings.region,
            locale=locale_hint,
            agent_id=agent_id,
            llm_model=self._settings.voice_llm_model,
            tts_voice_id=self._settings.voice_tts_voice,
        )
        self._spawn_pipeline(
            session_id=session_id,
            room_url=room_url,
            bot_token=bot_token,
            metadata=metadata,
            agent_manifest=agent_manifest,
        )

    def schedule_session_shutdown(self, session_id: str, delay_seconds: int) -> None:
        async def _shutdown_later() -> None:
            await asyncio.sleep(delay_seconds)
            task = self._tasks.get(session_id)
            if not task or task.done():
                return
            logger.info(
                "voice.session_migration_old_worker_shutdown",
                voice_session_id=session_id,
                delay_seconds=delay_seconds,
            )
            task.cancel()

        asyncio.create_task(_shutdown_later(), name=f"voice-migration-cleanup-{session_id}")

    def _spawn_pipeline(
        self,
        *,
        session_id: str,
        room_url: str,
        bot_token: str,
        metadata: VoiceMetadata,
        agent_manifest: dict[str, Any] | None,
    ) -> None:
        task = asyncio.create_task(
            run_daily_voice_pipeline(
                room_url=room_url,
                bot_token=bot_token,
                settings=self._settings,
                metadata=metadata,
                agent_manifest=agent_manifest,
                session_manager=self,
            ),
            name=f"daily-voice-{session_id}",
        )
        self._tasks[session_id] = task
        task.add_done_callback(lambda done: self._handle_done(session_id, done))

    async def _spawn_session_on_region(
        self,
        *,
        target_region: str,
        payload: dict[str, Any],
        subject: str,
    ) -> None:
        response = await self._internal_http_client.post(
            internal_spawn_url(target_region),
            json=payload,
            headers={
                "Authorization": f"Bearer {sign_voice_service_jwt(self._settings, subject=subject)}"
            },
        )
        response.raise_for_status()

    def _validate_provider_settings(self) -> None:
        missing = []
        if not self._settings.lumo_deepgram_api_key:
            missing.append("LUMO_DEEPGRAM_API_KEY")
        if not self._settings.sarvam_api_key:
            missing.append("SARVAM_API_KEY")
        if not self._settings.groq_api_key:
            missing.append("GROQ_API_KEY")
        if missing:
            raise ValueError(f"missing required provider secrets: {', '.join(missing)}")


async def run_daily_voice_pipeline(
    *,
    room_url: str,
    bot_token: str,
    settings: Settings,
    metadata: VoiceMetadata,
    agent_manifest: dict[str, Any] | None = None,
    session_manager: VoiceSessionManager | None = None,
) -> None:
    resolved_manifest = agent_manifest_for(
        agent_id=metadata.agent_id,
        provided_manifest=agent_manifest,
    )
    llm = pick_llm_service(agent_manifest=resolved_manifest, settings=settings)
    llm_provider = llm_provider_for(llm)
    llm_model = llm_model_for(llm, fallback=settings.voice_llm_model)
    metadata = replace(metadata, llm_provider=llm_provider, llm_model=llm_model)
    tracker = VoiceTurnTracker(metadata)
    cost_tracker = VoiceSessionCostTracker(llm_provider=llm_provider)
    session_span = get_tracer().start_span("voice.session")
    session_span.set_attribute("voice.session_id", metadata.voice_session_id)
    session_span.set_attribute("voice.agent_id", metadata.agent_id)
    session_span.set_attribute("voice.llm.provider", metadata.llm_provider)
    session_span.set_attribute("voice.llm.model", metadata.llm_model)
    session_span.set_attribute("fly.region", metadata.region)
    dispatcher = VoiceTurnDispatcher(
        gateway_url=settings.gateway_url,
        internal_token=settings.internal_token,
        metadata=metadata,
        tracker=tracker,
    )
    tracker.set_snapshot_dispatcher(dispatcher)
    migration_coordinator = (
        SessionMigrationCoordinator(
            session_manager=session_manager,
            metadata=metadata,
            agent_manifest=agent_manifest,
        )
        if session_manager
        else None
    )
    transport = DailyTransport(
        room_url,
        bot_token,
        "Orchet Voice Bot",
        DailyParams(
            api_key=settings.daily_api_key,
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=settings.voice_tts_sample_rate,
            audio_out_channels=1,
            transcription_enabled=False,
        ),
    )

    language_detector = SarvamStreamingLanguageDetector(
        api_key=settings.sarvam_api_key,
        model=settings.voice_sarvam_stt_model,
    )
    language_router = LanguageDetectionProcessor(
        tracker=tracker,
        detector=language_detector,
        sarvam_tts_speaker=settings.voice_sarvam_tts_speaker,
        deepgram_tts_voice=settings.voice_tts_voice,
        detection_seconds=settings.voice_language_detection_seconds,
        on_locale_detected=migration_coordinator.request_migration
        if migration_coordinator
        else None,
    )
    deepgram_stt = DeepgramSTTService(
        api_key=settings.lumo_deepgram_api_key,
        sample_rate=16000,
        live_options=LiveOptions(
            encoding="linear16",
            language="en-US",
            model=settings.voice_stt_model,
            channels=1,
            interim_results=True,
            smart_format=True,
            punctuate=True,
            endpointing=settings.voice_stt_endpointing_ms,
        ),
    )
    sarvam_stt = SarvamSTTService(
        api_key=settings.sarvam_api_key,
        target_language_code="unknown",
        model=settings.voice_sarvam_stt_model,
        sample_rate=16000,
    )
    stt = ParallelPipeline(
        [
            ProviderGateProcessor(
                tracker=tracker,
                provider="deepgram",
                selected_provider=lambda: tracker.stt_provider,
                gated_types=stt_gate_types(),
                name="orchet-deepgram-stt-gate",
            ),
            deepgram_stt,
        ],
        [
            ProviderGateProcessor(
                tracker=tracker,
                provider="sarvam",
                selected_provider=lambda: tracker.stt_provider,
                gated_types=stt_gate_types(),
                name="orchet-sarvam-stt-gate",
            ),
            sarvam_stt,
        ],
    )
    # Deepgram TTS — streaming WebSocket vs REST fallback.
    #
    # The REST path (pipecat.services.deepgram.DeepgramTTSService) has two
    # failure modes in our streaming pipeline:
    #
    #   aggregate_sentences=False → token-per-REST-call → choppy audio with
    #     fade-in / fade-out seam between every word. Verified in prod
    #     2026-05-13: "for every word it's pausing and speaking."
    #
    #   aggregate_sentences=True  → Pipecat's SimpleTextAggregator buffers
    #     the entire LLM response until a sentence-ending '.!?' OR the
    #     terminal LLMFullResponseEndFrame. Honeycomb trace
    #     voice_3f09eeac5e6f4cd3ae58a42bfd18ab47 showed
    #     voice.total.mouth_to_ear = 25.4 s. User stopped the call before
    #     audio arrived.
    #
    # The streaming WS path (DeepgramStreamingTTSService) wraps Aura-2's
    # /v1/speak WebSocket — text in, audio out, model-side smoothing. With
    # aggregate_sentences=False the WS still produces fluent audio because
    # the smoothing happens inside Deepgram's synthesis loop, not at REST
    # boundaries. Default; ORCHET_VOICE_DEEPGRAM_TTS_MODE=rest restores the
    # old path as a kill-switch if the new one misbehaves.
    if settings.voice_deepgram_tts_mode == "rest":
        deepgram_tts: FrameProcessor = DeepgramTTSService(
            api_key=settings.lumo_deepgram_api_key,
            voice=settings.voice_tts_voice,
            sample_rate=settings.voice_tts_sample_rate,
            encoding=settings.voice_tts_encoding,
            aggregate_sentences=True,
        )
    else:
        deepgram_tts = DeepgramStreamingTTSService(
            api_key=settings.lumo_deepgram_api_key,
            voice=settings.voice_tts_voice,
            sample_rate=settings.voice_tts_sample_rate,
            encoding=settings.voice_tts_encoding,
            aggregate_sentences=False,
        )
    # Sarvam Bulbul is also a streaming WS — same reasoning as the
    # Deepgram WS branch above. aggregate_sentences=True caused the 25 s
    # buffer regression; the streaming endpoint handles per-token text
    # smoothly on its own.
    sarvam_tts = SarvamTTSService(
        api_key=settings.sarvam_api_key,
        target_language_code=lambda: sarvam_locale_for(tracker.locale),
        model=settings.voice_sarvam_tts_model,
        speaker=settings.voice_sarvam_tts_speaker,
        sample_rate=settings.voice_tts_sample_rate,
        output_audio_codec=settings.voice_tts_encoding,
        aggregate_sentences=False,
    )
    tts = ParallelPipeline(
        [
            ProviderGateProcessor(
                tracker=tracker,
                provider="deepgram",
                selected_provider=lambda: tracker.tts_provider,
                gated_types=tts_gate_types(),
                name="orchet-deepgram-tts-gate",
            ),
            deepgram_tts,
        ],
        [
            ProviderGateProcessor(
                tracker=tracker,
                provider="sarvam",
                selected_provider=lambda: tracker.tts_provider,
                gated_types=tts_gate_types(),
                name="orchet-sarvam-tts-gate",
            ),
            sarvam_tts,
        ],
    )
    transport_output = transport.output()
    register_voice_tools(llm, dispatcher, transport_output)

    context = OpenAILLMContext.from_messages(
        [{"role": "system", "content": load_voice_prompt(metadata.locale)}]
    )
    context.set_tools(VOICE_TOOLS_SCHEMA)
    context.set_tool_choice("auto")
    context_aggregator = llm.create_context_aggregator(
        context,
        user_kwargs={"aggregation_timeout": 0.05},
    )

    pipeline = Pipeline(
        [
            transport.input(),
            AudioDurationCostProcessor(cost_tracker),
            ClientVADInterruptionProcessor(tracker, dispatcher),
            language_router,
            stt,
            STTSpanProcessor(tracker),
            LanguagePromptProcessor(tracker=tracker, context=context),
            context_aggregator.user(),
            llm,
            LLMSpanProcessor(tracker, metadata, cost_tracker),
            tts,
            TTSSpanProcessor(tracker, metadata, cost_tracker),
            MigrationFrameSenderProcessor(migration_coordinator, transport_output),
            transport_output,
            context_aggregator.assistant(),
        ]
    )
    task = PipelineTask(
        pipeline,
        params=build_voice_pipeline_params(settings),
        idle_timeout_secs=300,
    )
    runner = PipelineRunner(handle_sigint=False)
    try:
        await runner.run(task)
    finally:
        estimate = cost_tracker.estimate(
            stt_provider=tracker.stt_provider,
            tts_provider=tracker.tts_provider,
        )
        set_cost_span_attributes(
            session_span,
            estimate=estimate,
            llm_provider=metadata.llm_provider,
            stt_provider=tracker.stt_provider,
            tts_provider=tracker.tts_provider,
            locale=tracker.locale,
        )
        session_span.end()
        await dispatcher.aclose()


def build_voice_pipeline_params(settings: Settings) -> PipelineParams:
    return PipelineParams(
        allow_interruptions=True,
        audio_in_sample_rate=16000,
        audio_out_sample_rate=settings.voice_tts_sample_rate,
        enable_metrics=True,
        enable_usage_metrics=True,
    )


class SessionMigrationCoordinator:
    def __init__(
        self,
        *,
        session_manager: VoiceSessionManager | None,
        metadata: VoiceMetadata,
        agent_manifest: dict[str, Any] | None,
    ):
        self._session_manager = session_manager
        self._metadata = metadata
        self._agent_manifest = agent_manifest
        self._prepare_task: asyncio.Task[VoiceSessionMigrate | None] | None = None
        self._sent = False

    def request_migration(self, locale: str) -> None:
        if self._prepare_task or self._sent or not self._session_manager:
            return
        if not should_migrate_for_sarvam(self._metadata.region, locale):
            return
        target = pick_target_region(self._metadata.region)
        self._prepare_task = asyncio.create_task(
            self._prepare(target, locale),
            name=f"voice-migration-prepare-{self._metadata.voice_session_id}",
        )

    async def send_if_ready(self, transport_output: object) -> None:
        if self._sent or not self._prepare_task:
            return
        frame = await self._prepare_task
        if not frame:
            return
        await send_daily_app_message(transport_output, frame.model_dump(mode="json"))
        self._sent = True
        if self._session_manager:
            # PR1 chooses the simple v1 cleanup contract: trust the client's
            # reconnect path and cancel the old worker after a short grace window.
            self._session_manager.schedule_session_shutdown(
                self._metadata.voice_session_id,
                OLD_SESSION_GRACE_SECONDS,
            )

    async def _prepare(self, target_region: str, locale: str) -> VoiceSessionMigrate | None:
        if not self._session_manager:
            return None
        for target in _migration_targets(target_region):
            try:
                return await self._session_manager.migrate_session_to_region(
                    self._metadata.voice_session_id,
                    target,
                    metadata=self._metadata,
                    agent_manifest=self._agent_manifest,
                    locale_hint=locale,
                )
            except Exception as exc:
                logger.error(
                    "voice.session_migration_prepare_failed",
                    voice_session_id=self._metadata.voice_session_id,
                    target_region=target,
                    error=str(exc),
                )
        logger.error(
            "voice.session_migration_staying_current_region",
            voice_session_id=self._metadata.voice_session_id,
            current_region=self._metadata.region,
            locale=locale,
        )
        return None


class MigrationFrameSenderProcessor(FrameProcessor):
    def __init__(
        self,
        coordinator: SessionMigrationCoordinator | None,
        transport_output: object,
    ):
        super().__init__(name="orchet-migration-frame-sender")
        self._coordinator = coordinator
        self._transport_output = transport_output

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, LLMFullResponseEndFrame)
            and self._coordinator
        ):
            await self._coordinator.send_if_ready(self._transport_output)
        await self.push_frame(frame, direction)


async def send_daily_app_message(transport_output: object, message: dict[str, Any]) -> None:
    sender = transport_output
    send_message = getattr(sender, "send_message", None)
    if not send_message:
        raise RuntimeError("Daily transport output does not expose send_message")
    await send_message(TransportMessageUrgentFrame(message))


def daily_geo_region_for_fly_region(fly_region: str) -> str | None:
    return {
        "bom": "ap-south-1",
        "sin": "ap-southeast-1",
    }.get(fly_region.strip().lower())


def internal_spawn_url(target_region: str) -> str:
    return f"http://{target_region.strip().lower()}.{INTERNAL_APP_NAME}.internal:8080/internal/spawn_session"


def _migration_targets(primary: str) -> tuple[str, ...]:
    primary = primary.strip().lower()
    if primary == "bom":
        return ("bom", "sin")
    if primary == "sin":
        return ("sin", "bom")
    return ("bom", "sin")


def register_voice_tools(
    llm: Any,
    dispatcher: Any,
    transport_output: object,
) -> None:
    async def handler(
        function_name: str,
        tool_call_id: str,
        arguments: object,
        service: Any,
        context: object,
        result_callback: Callable[..., Awaitable[None]],
    ) -> None:
        del tool_call_id, context
        outcome = await dispatcher.dispatch(
            function_name,
            arguments if isinstance(arguments, dict) else {},
            transport=transport_output,
        )
        if outcome.spoken_text:
            await service.push_frame(TTSTextFrame(outcome.spoken_text), FrameDirection.DOWNSTREAM)
        await result_callback(
            outcome.function_result,
            properties=FunctionCallResultProperties(run_llm=outcome.run_llm),
        )

    for schema in VOICE_FUNCTION_SCHEMAS:
        llm.register_function(schema.name, handler, cancel_on_interruption=True)
