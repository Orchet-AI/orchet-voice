from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import structlog

logger = structlog.get_logger()


@dataclass
class VoiceTurnTiming:
    stt_first_partial_ms: int | None = None
    stt_final_ms: int | None = None
    llm_ttft_ms: int | None = None
    tts_first_chunk_ms: int | None = None


@dataclass(frozen=True)
class VoiceTurnTranscript:
    session_id: str
    turn_id: str
    user_id: str
    user_text: str
    assistant_text: str
    timing: VoiceTurnTiming


class DeferredTranscriptPersistence:
    def __init__(
        self,
        *,
        gateway_url: str,
        internal_token: str,
        enabled: bool,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._gateway_url = gateway_url.rstrip("/")
        self._internal_token = internal_token
        self._enabled = enabled
        self._http_client = http_client
        self._owns_client = http_client is None
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, transcript: VoiceTurnTranscript) -> None:
        if not transcript.user_text or not transcript.assistant_text:
            return

        task = asyncio.create_task(
            self._persist(transcript),
            name=f"voice-persist-{transcript.session_id}-{transcript.turn_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._owns_client and self._http_client:
            await self._http_client.aclose()

    async def _persist(self, transcript: VoiceTurnTranscript) -> None:
        payload = self._payload(transcript)

        if not self._enabled:
            logger.info(
                "voice.persistence.deferred",
                voice_session_id=transcript.session_id,
                voice_turn_id=transcript.turn_id,
            )
            return

        if not self._internal_token:
            logger.warning(
                "voice.persistence.failed",
                voice_session_id=transcript.session_id,
                voice_turn_id=transcript.turn_id,
                error="ORCHET_INTERNAL_TOKEN missing",
            )
            return

        client = self._client()
        try:
            response = await client.post(
                f"/sessions/{transcript.session_id}/messages",
                json=payload,
                headers={"Authorization": f"Bearer {self._internal_token}"},
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "voice.persistence.failed",
                voice_session_id=transcript.session_id,
                voice_turn_id=transcript.turn_id,
                error=str(exc),
            )

    def _client(self) -> httpx.AsyncClient:
        if not self._http_client:
            self._http_client = httpx.AsyncClient(base_url=self._gateway_url, timeout=3.0)
        return self._http_client

    @staticmethod
    def _payload(transcript: VoiceTurnTranscript) -> dict[str, object]:
        timing = transcript.timing
        return {
            "session_id": transcript.session_id,
            "turn_id": transcript.turn_id,
            "user_id": transcript.user_id,
            "channel": "voice",
            "messages": [
                {"role": "user", "content": transcript.user_text},
                {"role": "assistant", "content": transcript.assistant_text},
            ],
            "latency_ms": {
                "stt_first_partial": timing.stt_first_partial_ms,
                "stt_final": timing.stt_final_ms,
                "llm_ttft": timing.llm_ttft_ms,
                "tts_first_chunk": timing.tts_first_chunk_ms,
            },
        }
