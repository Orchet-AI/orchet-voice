from __future__ import annotations

from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)

from tests.test_pipeline_helpers import FakeTracer, collect_frames
from voice.pipeline import STT_STREAM_SPAN_NAME, STTSpanProcessor, VoiceMetadata, VoiceTurnTracker


async def test_stt_stream_span_records_partial_and_final(fake_tracer: FakeTracer) -> None:
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = STTSpanProcessor(tracker)

    await collect_frames(processor, UserStartedSpeakingFrame())
    await collect_frames(processor, InterimTranscriptionFrame("hello", "user", "ts"))
    await collect_frames(processor, TranscriptionFrame("hello there", "user", "ts"))

    span = next(span for span in fake_tracer.spans if span.name == STT_STREAM_SPAN_NAME)
    assert tracker.current is not None
    assert span.ended
    assert span.attributes["voice.session_id"] == "voice_test"
    assert span.attributes["voice.turn_id"] == tracker.current.turn_id
    assert span.attributes["client.kind"] == "web"
    assert span.attributes["voice.stt.partial_count"] == 1
    assert isinstance(span.attributes["voice.stt.first_partial_ms"], int)
    assert isinstance(span.attributes["voice.stt.final_ms"], int)
