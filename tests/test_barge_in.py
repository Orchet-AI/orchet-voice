from __future__ import annotations

from pipecat.frames.frames import (
    StartInterruptionFrame,
    StopInterruptionFrame,
    TransportMessageUrgentFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)

from tests.test_pipeline_helpers import FakeTracer, collect_frames
from voice.pipeline import (
    TOTAL_MOUTH_TO_EAR_SPAN_NAME,
    TTS_STREAM_SPAN_NAME,
    ClientVADInterruptionProcessor,
    VoiceMetadata,
    VoiceTurnTracker,
    _now,
)


async def test_barge_in_event_cancels_tts_and_starts_new_turn(fake_tracer: FakeTracer) -> None:
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    turn = tracker.start_turn("turn_tokyo")
    turn.tts_span = tracker.start_stage_span(TTS_STREAM_SPAN_NAME)
    turn.tts_started_at = _now() - 0.05
    processor = ClientVADInterruptionProcessor(tracker)

    pushed = await collect_frames(
        processor,
        TransportMessageUrgentFrame(
            {
                "type": "barge_in",
                "state": "speech_started",
                "turn_id": "turn_london",
            }
        ),
    )

    pushed_types = [type(frame) for frame, _ in pushed]
    assert pushed_types == [StartInterruptionFrame, UserStartedSpeakingFrame]
    assert tracker.current is not None
    assert tracker.current.turn_id == "turn_london"
    tts_span = next(span for span in fake_tracer.spans if span.name == TTS_STREAM_SPAN_NAME)
    assert "voice.tts.barge_in_ms" in tts_span.attributes
    assert tts_span.attributes["voice.interrupted"] is True


async def test_barge_in_speech_end_flushes_turn_to_stt(fake_tracer: FakeTracer) -> None:
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    tracker.start_turn("turn_london")
    processor = ClientVADInterruptionProcessor(tracker)

    pushed = await collect_frames(
        processor,
        TransportMessageUrgentFrame({"type": "barge_in", "state": "speech_ended"}),
    )

    pushed_types = [type(frame) for frame, _ in pushed]
    assert pushed_types == [UserStoppedSpeakingFrame, StopInterruptionFrame]


async def test_barge_in_sets_tts_barge_in_ms_when_tts_active(
    fake_tracer: FakeTracer,
) -> None:
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    turn = tracker.start_turn("turn_london")
    turn.tts_span = tracker.start_stage_span(TTS_STREAM_SPAN_NAME)
    turn.tts_started_at = _now() - 0.05

    tracker.interrupt_active_spans()

    total_span = next(
        span for span in fake_tracer.spans if span.name == TOTAL_MOUTH_TO_EAR_SPAN_NAME
    )
    tts_span = next(span for span in fake_tracer.spans if span.name == TTS_STREAM_SPAN_NAME)
    tts_barge_in_ms = tts_span.attributes["voice.tts.barge_in_ms"]

    assert isinstance(tts_barge_in_ms, int)
    assert tts_barge_in_ms >= 50
    assert tts_barge_in_ms < 200
    assert total_span.attributes["voice.tts.barge_in_ms"] == tts_barge_in_ms
