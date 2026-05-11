from __future__ import annotations

from pipecat.frames.frames import (
    StartInterruptionFrame,
    StopInterruptionFrame,
    TransportMessageUrgentFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)

from tests.test_pipeline_helpers import FakeTracer, collect_frames
from voice.pipeline import ClientVADInterruptionProcessor, VoiceMetadata, VoiceTurnTracker


async def test_barge_in_event_cancels_tts_and_starts_new_turn(fake_tracer: FakeTracer) -> None:
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
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
