from __future__ import annotations

import asyncio
import contextlib
import random
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
    LLMTextFrame,
    TranscriptionFrame,
    TransportMessageUrgentFrame,
    TTSTextFrame,
)
from pipecat.pipeline.parallel_pipeline import ParallelPipeline
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic import AnthropicLLMContext
from pipecat.services.deepgram import DeepgramSTTService, DeepgramTTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport

from voice.auth import AuthenticatedUser
from voice.brain import create_brain_memory_adapter
from voice.internal_auth import sign_voice_service_jwt
from voice.obs.cost import VoiceSessionCostTracker, set_cost_span_attributes
from voice.obs.tracing import get_tracer
from voice.pipeline import (
    AudioDurationCostProcessor,
    ClientVADInterruptionProcessor,
    LLMSpanProcessor,
    MarkdownStripperProcessor,
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
from voice.tools.builtin_tools import BUILTIN_TOOL_HANDLERS
from voice.voice_turn_dispatcher import VoiceTurnDispatcher

logger = structlog.get_logger()

MIGRATION_VALID_FOR_SECONDS = 120
OLD_SESSION_GRACE_SECONDS = 10
INTERNAL_APP_NAME = "orchet-voice"

# Pre-flight ack phrases — spoken via TTS the moment a backend
# dispatch starts so the user hears the bot inside ~500ms instead of
# waiting silently for the 5–25 s /voice/turn round-trip.
#
# Production 2026-05-15: user reported voice as "doing nothing" and
# learned to greet ("Hey Orchet") because greetings hit the local
# Haiku path (fast) while real questions hit agent_query (silent for
# 20 s until the full backend response arrived). This pump primes
# the audio channel so the latency stays invisible.
#
# Kept SHORT and natural — three to five syllables, contractions
# only, no "please hold" / "let me see if I can find that" lengths.
# The actual answer follows; the filler shouldn't compete for the
# user's attention or pad the turn.
_BACKEND_DISPATCH_ACK_PHRASES: tuple[str, ...] = (
    "On it.",
    "Let me check.",
    "One sec.",
    "Looking that up.",
    "Hold on, checking.",
    "Got it, give me a moment.",
)


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
        # The room must exist before tokens can be issued against it,
        # but the bot and client meeting tokens have no data dependency
        # on each other — both just bind to room.name. Issuing them in
        # parallel saves one transcontinental REST round-trip
        # (~300-700ms) on every session-create, which lands directly on
        # the user-perceived "Thinking → Listening" gap when they tap
        # "Tap to talk".
        room = await daily.create_room(room_name, expires_at)
        bot_token, client_token = await asyncio.gather(
            daily.create_meeting_token(
                room.name,
                expires_at,
                is_owner=True,
                user_name="Orchet Voice Bot",
            ),
            daily.create_meeting_token(
                room.name,
                expires_at,
                is_owner=False,
                user_name=user.email or user.user_id,
            ),
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
        # Open the Deepgram WebSocket in the background NOW so turn-1
        # synthesize() doesn't pay the ~300-500ms connect cost on the
        # user-visible mouth-to-ear path. The persistent connection
        # design already smooths turns ≥2; this closes the cold-start
        # hole. Fire-and-forget — pipeline boot is intentionally not
        # blocked on Deepgram (an outage there shouldn't keep the room
        # from connecting; lazy reconnect handles it).
        asyncio.create_task(deepgram_tts.prewarm())
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
    register_voice_tools(
        llm,
        dispatcher,
        transport_output,
        settings=settings,
        metadata=metadata,
    )

    # Build the right context shape for the configured LLM. Anthropic's
    # Messages API requires the system prompt as a top-level `system`
    # param, NOT as a {role: "system"} message inside `messages`. Pipecat's
    # AnthropicLLMService crashes (HTTP 400) when it sees a role=system
    # entry in messages — and the LanguagePromptProcessor below also
    # injects one on locale changes if the context is OpenAI-shaped.
    # Constructing AnthropicLLMContext directly with `system=...` puts
    # the prompt where it belongs from turn 1 onward.
    voice_prompt = load_voice_prompt(metadata.locale)

    # Phase 1 of the Brain-for-voice initiative: fetch the user's
    # USER CONTEXT block (profile + recent facts, pre-rendered by
    # orchet-backend) and append to the locale prompt before the LLM
    # context is constructed. Fail-open at 500ms — a slow Brain
    # produces a session that knows less, never one that won't start.
    # See ADR-013 + docs/strategy/BRAIN-FOR-VOICE.md.
    user_context_message = await _fetch_user_context_safely(
        settings=settings,
        metadata=metadata,
    )
    if user_context_message:
        voice_prompt = f"{voice_prompt}\n\n{user_context_message}"

    provider = llm_provider_for(llm)
    context: OpenAILLMContext
    if provider == "anthropic":
        context = AnthropicLLMContext(messages=[], system=voice_prompt)
    else:
        context = OpenAILLMContext.from_messages([{"role": "system", "content": voice_prompt}])
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
            MarkdownStripperProcessor(),
            tts,
            TTSSpanProcessor(tracker, metadata, cost_tracker),
            MigrationFrameSenderProcessor(migration_coordinator, transport_output),
            TranscriptAppMessageProcessor(transport_output, metadata, tracker),
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


async def _fetch_user_context_safely(
    *,
    settings: Settings,
    metadata: VoiceMetadata,
) -> str | None:
    """Phase 1 of Brain-for-voice: pull the user's USER CONTEXT block
    from orchet-backend POST /voice/session-context and return the
    pre-rendered system-prompt slice for appending to the locale
    prompt.

    Fail-open at every layer — a misconfigured / unreachable / slow
    backend produces a session with no user context, never a session
    that won't start. The base locale prompt remains the contract.

    Telemetry lands on a dedicated `voice.context.fetch` span (not the
    per-turn total_span). Two reasons: (1) turn.total_span is the
    user-facing mouth-to-ear timer (#6 backlog) — context-fetch
    attributes would distort the latency dashboards; (2) at session
    start there is NO turn yet, so the previous code's
    `tracker.ensure_turn()` side-effect was prematurely creating a
    turn-#1 + total_span before the user even spoke. Honeycomb
    attribute names (voice.context.*) are preserved so existing
    dashboards keep working — they just live under a new span name.
    """
    if not settings.orchet_ml_brain_url or not settings.lumo_ml_service_jwt_secret:
        # Brain URL or JWT secret not configured — fail open silently.
        # Voice still starts; base locale prompt remains the contract.
        return None
    if not metadata.user_id or metadata.user_id == "anon":
        return None

    adapter = create_brain_memory_adapter(
        brain_url=settings.orchet_ml_brain_url,
        jwt_secret=settings.lumo_ml_service_jwt_secret,
    )
    started = time.perf_counter()
    try:
        ctx = await adapter.get_session_context(
            user_id=metadata.user_id,
            voice_session_id=metadata.voice_session_id,
            agent_id=metadata.agent_id,
            locale=metadata.locale,
        )
    except Exception as exc:  # noqa: BLE001
        # Defense-in-depth — the adapter is already fail-open, but if
        # it raises (e.g., misconfigured httpx) we still want the
        # session to start.
        logger.warning(
            "voice.brain.memory.unexpected_error",
            voice_session_id=metadata.voice_session_id,
            error=str(exc)[:200],
        )
        with contextlib.suppress(Exception):
            await adapter.aclose()  # type: ignore[attr-defined]
        return None
    finally:
        # Single-shot use — close the underlying httpx client so we
        # don't leak connections per session.
        with contextlib.suppress(Exception):
            await adapter.aclose()  # type: ignore[attr-defined]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    # Telemetry lands on a dedicated `voice.context.fetch` span rather
    # than turn.total_span. Two reasons: (1) turn.total_span is the
    # USER-FACING mouth-to-ear timer (#6 backlog) — polluting it with
    # backend instrumentation distorts the latency dashboards; (2) a
    # dedicated span is queryable in Honeycomb by name, doesn't fight
    # for attribute namespace, and shows up as a child segment in the
    # trace waterfall where it's actually useful for triage. Fail-open
    # via suppress() — telemetry must never block voice session start.
    with contextlib.suppress(Exception):
        ctx_span = get_tracer().start_span("voice.context.fetch")
        ctx_span.set_attribute("voice.session_id", metadata.voice_session_id)
        ctx_span.set_attribute("voice.agent_id", metadata.agent_id)
        ctx_span.set_attribute("voice.context.profile_loaded", ctx.profile_loaded)
        ctx_span.set_attribute("voice.context.facts_count", ctx.facts_count)
        ctx_span.set_attribute("voice.context.fetch_ms", elapsed_ms)
        ctx_span.set_attribute("voice.context.server_compose_ms", ctx.elapsed_ms)
        ctx_span.set_attribute("voice.context.partial", ctx.partial)
        ctx_span.end()

    logger.info(
        "voice.brain.memory.fetched",
        voice_session_id=metadata.voice_session_id,
        profile_loaded=ctx.profile_loaded,
        facts_count=ctx.facts_count,
        client_elapsed_ms=elapsed_ms,
        server_elapsed_ms=ctx.elapsed_ms,
        partial=ctx.partial,
    )

    return ctx.system_message if ctx.has_content else None


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


class TranscriptAppMessageProcessor(FrameProcessor):
    """Emit Daily app-messages so the web/iOS client can render the
    live conversation transcript in the chat thread.

    Why this exists
    ---------------
    Pipecat already maintains the user/assistant text internally — the
    STT service emits ``TranscriptionFrame`` for final user utterances
    and the LLM emits ``LLMTextFrame`` deltas + ``LLMFullResponseEndFrame``
    at the end of a response. That's enough for the voice service to do
    its job (route tools, drive TTS) but the connected client never
    sees those frames. Before this processor, the only Daily app-messages
    going to the client were ``voice_show_confirmation`` and
    ``voice_session_migrate``. So in pure Daily-WebRTC voice mode there
    was no live transcript display — the chat thread sat empty while
    the user and Orchet talked. Production repro 2026-05-14, reported
    as "the transcript is not coming while the conversation is
    happening between user and voice agent".

    Wire shape
    ----------
    Three message kinds — names mirror the existing
    ``voice_show_confirmation`` convention so the web data channel can
    discriminate cheaply:

      voice_user_transcript
          { type, voice_session_id, turn_id, text }
        Emitted on final TranscriptionFrame only. We deliberately skip
        InterimTranscriptionFrame: the chat thread renders bubbles, and
        a bubble that keeps mutating mid-utterance reads worse than one
        bubble that lands when the user pauses.

      voice_assistant_transcript_delta
          { type, voice_session_id, turn_id, text }
        Emitted on each LLMTextFrame so the assistant bubble streams in
        token-by-token, matching the chat surface's UX. ``text`` is the
        delta only — NOT the cumulative response. Concatenation lives
        on the client.

      voice_assistant_transcript_final
          { type, voice_session_id, turn_id, text }
        Emitted on LLMFullResponseEndFrame. ``text`` is the full
        assembled assistant response from the tracker (cumulative). The
        client uses this to detect end-of-stream and to reconcile in
        case any delta dropped on the wire.

    Placement
    ---------
    Pipeline order: must sit DOWNSTREAM of the STT and LLM span
    processors so user_transcript/assistant_text accumulate first, but
    UPSTREAM of ``transport_output`` so the app-message goes out the
    same Daily socket the audio frames use. The existing
    ``MigrationFrameSenderProcessor`` sits in that same slot for the
    same reason — we slot in right next to it.

    Failure mode
    ------------
    ``send_daily_app_message`` raises if the transport output isn't
    ready (no Daily socket yet). We catch broadly and log — a missed
    transcript update is not worth tearing down the call.
    """

    def __init__(
        self,
        transport_output: object,
        metadata: VoiceMetadata,
        tracker: VoiceTurnTracker,
    ):
        super().__init__(name="orchet-transcript-app-message")
        self._transport_output = transport_output
        self._metadata = metadata
        self._tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TranscriptionFrame):
                text = frame.text.strip() if isinstance(frame.text, str) else ""
                if text:
                    await self._safe_send(
                        {
                            "type": "voice_user_transcript",
                            "voice_session_id": self._metadata.voice_session_id,
                            "turn_id": self._current_turn_id(),
                            "text": text,
                        }
                    )
            elif isinstance(frame, LLMTextFrame):
                # Streaming chunk — send the raw delta. Don't strip
                # whitespace: leading/trailing spaces are how the
                # assistant's tokens glue into prose on the client.
                if frame.text:
                    await self._safe_send(
                        {
                            "type": "voice_assistant_transcript_delta",
                            "voice_session_id": self._metadata.voice_session_id,
                            "turn_id": self._current_turn_id(),
                            "text": frame.text,
                        }
                    )
            elif isinstance(frame, LLMFullResponseEndFrame):
                final_text = ""
                turn = self._tracker.current
                if turn:
                    final_text = (turn.assistant_text or "").strip()
                await self._safe_send(
                    {
                        "type": "voice_assistant_transcript_final",
                        "voice_session_id": self._metadata.voice_session_id,
                        "turn_id": self._current_turn_id(),
                        "text": final_text,
                    }
                )
        await self.push_frame(frame, direction)

    def _current_turn_id(self) -> str | None:
        turn = self._tracker.current
        return turn.turn_id if turn else None

    async def _safe_send(self, message: dict[str, Any]) -> None:
        try:
            await send_daily_app_message(self._transport_output, message)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "voice.transcript_app_message_failed",
                voice_session_id=self._metadata.voice_session_id,
                message_type=message.get("type"),
                error=str(exc),
            )


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
    *,
    settings: Settings,
    metadata: VoiceMetadata,
) -> None:
    """Wire every tool the LLM might call.

    Two execution paths:

    * **Built-in tools** (``current_time``, ``current_date``,
      ``current_weather``, ``web_search``) — handled locally in the
      voice service via ``voice/tools/builtin_tools.py``. No
      ``/voice/turn`` round-trip, no orchet-backend dependency, answer
      in milliseconds.

    * **Backend tools** (Gmail, Calendar, Spotify, Duffel, etc.) —
      dispatched through the ``VoiceTurnDispatcher`` to
      orchet-backend's ``/voice/turn`` endpoint where the full MCP
      catalog lives.

    The LLM doesn't know the difference — it sees one flat tool list
    from ``VOICE_TOOLS_SCHEMA``. The split is purely an implementation
    detail of where each tool actually runs.
    """

    async def handler(
        function_name: str,
        tool_call_id: str,
        arguments: object,
        service: Any,
        context: object,
        result_callback: Callable[..., Awaitable[None]],
    ) -> None:
        del tool_call_id, context
        args = arguments if isinstance(arguments, dict) else {}

        # ----- Built-in fast path ---------------------------------------
        local_handler = BUILTIN_TOOL_HANDLERS.get(function_name)
        if local_handler is not None:
            # Per-call execution context for handlers that need to
            # call orchet-backend (marketplace find/install). Plain
            # handlers (current_time, weather, web_search) ignore it
            # via their **kwargs catch-all.
            ctx: dict[str, Any] = {
                "user_id": metadata.user_id,
                "session_id": metadata.voice_session_id,
                "gateway_url": settings.gateway_url,
                "internal_token": sign_voice_service_jwt(
                    settings,
                    subject=metadata.user_id,
                ),
            }
            try:
                result = await local_handler(args, ctx=ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "voice.tools.builtin_handler_failed",
                    function=function_name,
                    error=str(exc)[:200],
                )
                result = {"error": f"{function_name} failed: {exc}"}
            await result_callback(
                result,
                properties=FunctionCallResultProperties(run_llm=True),
            )
            return

        # ----- Backend dispatch -----------------------------------------
        # Push an immediate spoken ack BEFORE the dispatch so the user
        # hears the bot within ~500ms even when /voice/turn takes the
        # full 20-25s for agent_query routes. Pipecat serializes TTS
        # output, so the filler finishes before the actual answer
        # plays — no overlap risk. The phrase is intentionally short
        # (≤5 syllables) so it doesn't pad the turn when the backend
        # comes back fast.
        #
        # Suppressed for snapshot_interrupted dispatches: those are
        # background telemetry calls that the user is never waiting on,
        # so an ack would be confusing. Today only the foreground tool
        # path reaches this branch (via the LLM tool-call handler);
        # any future caller that needs silent dispatch should call
        # dispatcher.dispatch() directly, not through this handler.
        ack_phrase = random.choice(_BACKEND_DISPATCH_ACK_PHRASES)
        await service.push_frame(
            TTSTextFrame(ack_phrase),
            FrameDirection.DOWNSTREAM,
        )

        outcome = await dispatcher.dispatch(
            function_name,
            args,
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
