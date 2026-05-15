from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from pipecat.frames.frames import TransportMessageUrgentFrame

from voice.obs.tracing import get_tracer
from voice.pipeline import VoiceMetadata, VoiceTurnTracker

logger = structlog.get_logger()


@dataclass(frozen=True)
class VoiceTurnDispatchOutcome:
    function_result: dict[str, Any]
    spoken_text: str | None
    run_llm: bool


class VoiceTurnDispatcher:
    def __init__(
        self,
        *,
        gateway_url: str,
        internal_token: str,
        metadata: VoiceMetadata,
        tracker: VoiceTurnTracker,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._gateway_url = gateway_url.rstrip("/")
        self._internal_token = internal_token
        self._metadata = metadata
        self._tracker = tracker
        self._http_client = http_client
        self._owns_client = http_client is None
        self._pending_confirmation_spans: dict[str, Span] = {}

    async def dispatch(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        transport: Any,
    ) -> VoiceTurnDispatchOutcome:
        turn = self._tracker.ensure_turn()
        idempotency_key = uuid7()
        started = time.perf_counter()
        span = self._start_outbound_span(tool_name)
        keep_span_open = False
        span.set_attribute("voice.channel", "voice")
        span.set_attribute("voice.tool_call.name", tool_name)
        span.set_attribute("voice.requires_visual_confirmation", False)

        try:
            response = await self._client().post(
                "/voice/turn",
                json={
                    "session_id": self._metadata.voice_session_id,
                    "turn_id": turn.turn_id,
                    "user_id": self._metadata.user_id,
                    "channel": "voice",
                    "locale": self._tracker.locale,
                    "agent_id": self._metadata.agent_id,
                    "turn_index": turn.turn_index,
                    "transcript_partial": turn.user_transcript,
                    "tool_call": {"name": tool_name, "arguments": arguments},
                    "context": {
                        "region": self._metadata.region,
                        "device_kind": self._metadata.client_kind,
                        "client_ip_region": self._metadata.region,
                    },
                },
                headers={
                    "Authorization": f"Bearer {self._internal_token}",
                    "Idempotency-Key": idempotency_key,
                },
            )
            response.raise_for_status()
            latency_ms = _elapsed_ms(started)
            payload = response.json()
            outcome = str(payload.get("outcome") or "")
            span.set_attribute("voice.outcome", outcome)
            span.set_attribute("voice.latency_ms", latency_ms)

            if outcome == "executed":
                return self._executed_outcome(payload, span)
            if outcome == "requires_visual_confirmation":
                confirmation_outcome = await self._confirmation_outcome(
                    payload, span, transport=transport
                )
                keep_span_open = bool(confirmation_outcome.function_result.get("confirmation_id"))
                return confirmation_outcome
            if outcome == "denied":
                return self._denied_outcome(payload, span)

            span.set_status(Status(StatusCode.ERROR, f"unexpected outcome: {outcome}"))
            return VoiceTurnDispatchOutcome(
                function_result={"denied": True, "reason_code": "voice_turn_bad_response"},
                spoken_text="I couldn't complete that. Please try again in the app.",
                run_llm=False,
            )
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            logger.warning(
                "voice.turn.outbound_failed",
                voice_session_id=self._metadata.voice_session_id,
                voice_turn_id=turn.turn_id,
                tool_name=tool_name,
                error=str(exc),
            )
            return VoiceTurnDispatchOutcome(
                function_result={"denied": True, "reason_code": "voice_turn_outbound_failed"},
                spoken_text="I'm having trouble reaching Orchet. Please try again in a moment.",
                run_llm=False,
            )
        finally:
            if not keep_span_open:
                span.end()

    async def snapshot_interrupted(self, snapshot: dict[str, Any]) -> None:
        payload = {
            "session_id": self._metadata.voice_session_id,
            "turn_id": snapshot.get("turn_id"),
            "user_id": self._metadata.user_id,
            "channel": "voice",
            "locale": self._tracker.locale,
            "agent_id": self._metadata.agent_id,
            "turn_index": snapshot.get("turn_index"),
            "transcript_partial": snapshot.get("user_text", ""),
            "interrupted": True,
            "user_text": snapshot.get("user_text", ""),
            "assistant_partial_text": snapshot.get("assistant_partial_text", ""),
            "cancel_at_ms": snapshot.get("cancel_at_ms"),
            "context": {
                "region": self._metadata.region,
                "device_kind": self._metadata.client_kind,
                "client_ip_region": self._metadata.region,
            },
        }
        try:
            response = await self._client().post(
                "/voice/turn",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._internal_token}",
                    "Idempotency-Key": uuid7(),
                },
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "voice.turn.interrupted_snapshot_failed",
                voice_session_id=self._metadata.voice_session_id,
                voice_turn_id=snapshot.get("turn_id"),
                error=str(exc),
            )

    def resolve_confirmation(self, confirmation_id: str, result: str) -> None:
        span = self._pending_confirmation_spans.pop(confirmation_id, None)
        if not span:
            return
        span.set_attribute("voice.confirm.resolved", result)
        span.end()

    async def aclose(self) -> None:
        for span in self._pending_confirmation_spans.values():
            span.set_attribute("voice.confirm.resolved", "unresolved")
            span.end()
        self._pending_confirmation_spans.clear()
        if self._owns_client and self._http_client:
            await self._http_client.aclose()

    def _executed_outcome(self, payload: dict[str, Any], span: Span) -> VoiceTurnDispatchOutcome:
        hint = _string_or_none(payload.get("voice_message_hint"))
        return VoiceTurnDispatchOutcome(
            function_result={"outcome": "executed", "result": payload.get("result")},
            spoken_text=hint,
            run_llm=hint is None,
        )

    async def _confirmation_outcome(
        self,
        payload: dict[str, Any],
        span: Span,
        *,
        transport: Any,
    ) -> VoiceTurnDispatchOutcome:
        confirmation_id = _string_or_none(payload.get("confirmation_id")) or ""
        confirmation_payload = _dict_or_empty(payload.get("confirmation_payload"))
        voice_message = _string_or_none(payload.get("voice_message"))
        span.set_attribute("voice.requires_visual_confirmation", True)
        await send_daily_message(
            transport,
            {
                "type": "show_confirmation",
                "voice_session_id": self._metadata.voice_session_id,
                "turn_id": self._tracker.ensure_turn().turn_id,
                "confirmation_id": confirmation_id,
                "confirmation_payload": confirmation_payload,
                "expires_at": confirmation_payload.get("expires_at"),
            },
        )
        if confirmation_id:
            self._pending_confirmation_spans[confirmation_id] = span
        return VoiceTurnDispatchOutcome(
            function_result={"deferred": True, "confirmation_id": confirmation_id},
            spoken_text=voice_message,
            run_llm=False,
        )

    def _denied_outcome(self, payload: dict[str, Any], span: Span) -> VoiceTurnDispatchOutcome:
        reason_code = _string_or_none(payload.get("reason_code")) or "voice_turn_denied"
        span.set_attribute("voice.reason_code", reason_code)
        return VoiceTurnDispatchOutcome(
            function_result={"denied": True, "reason_code": reason_code},
            spoken_text=_string_or_none(payload.get("voice_message")),
            run_llm=False,
        )

    def _start_outbound_span(self, tool_name: str) -> Span:
        turn = self._tracker.ensure_turn()
        parent = trace.set_span_in_context(turn.total_span) if turn.total_span else None
        span = get_tracer().start_span("voice.turn.outbound", context=parent)
        span.set_attribute("voice.session_id", self._metadata.voice_session_id)
        span.set_attribute("voice.turn_id", turn.turn_id)
        span.set_attribute("client.kind", self._metadata.client_kind)
        span.set_attribute("voice.tool_call.name", tool_name)
        return span

    def _client(self) -> httpx.AsyncClient:
        if not self._http_client:
            # Backend's /voice/turn runs the full Claude tool-use loop
            # (often web_search + memory_recall + Anthropic streaming)
            # and legitimately takes 5–25 s for agent_query routes.
            # A flat 5 s timeout was firing on every non-trivial turn,
            # leaving the voice pipeline with a partial response that
            # came out as choppy / unintelligible audio.
            #
            # Split timeouts keep the connect / write / pool slots
            # tight so a real backend outage still fails fast, but
            # reads can absorb a slow Claude turn.
            # Honeycomb 2026-05-15: voice.orchestrator.voice_turn
            # p95 = 20 s, max = 26.6 s. 30 s read budget covers that
            # with margin; beyond 30 s the user has given up anyway.
            self._http_client = httpx.AsyncClient(
                base_url=self._gateway_url,
                timeout=httpx.Timeout(
                    connect=2.0,
                    read=30.0,
                    write=10.0,
                    pool=5.0,
                ),
            )
        return self._http_client


async def send_daily_message(transport: Any, message: dict[str, Any]) -> None:
    sender = transport
    if not hasattr(sender, "send_message") and hasattr(transport, "output"):
        sender = transport.output()
    send_message = getattr(sender, "send_message", None)
    if not send_message:
        raise RuntimeError("Daily transport does not expose send_message")
    await send_message(TransportMessageUrgentFrame(message))


def uuid7() -> str:
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return str(UUID(int=value))


def _elapsed_ms(started_at: float) -> int:
    return max(0, round((time.perf_counter() - started_at) * 1000))


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
