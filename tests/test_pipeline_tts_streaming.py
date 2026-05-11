from __future__ import annotations

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSTextFrame,
)

from tests.test_pipeline_helpers import FakeTracer, collect_frames
from voice.pipeline import (
    TOTAL_MOUTH_TO_EAR_SPAN_NAME,
    TTS_STREAM_SPAN_NAME,
    TTSSpanProcessor,
    VoiceMetadata,
    VoiceTurnTracker,
)


async def test_tts_stream_span_records_first_chunk_and_total_chars(
    fake_tracer: FakeTracer,
) -> None:
    metadata = VoiceMetadata(
        voice_session_id="voice_test",
        user_id="user_test",
        tts_voice_id="aura-2-andromeda-en",
    )
    tracker = VoiceTurnTracker(metadata)
    tracker.start_turn("turn_test")
    processor = TTSSpanProcessor(tracker, metadata)

    await collect_frames(processor, TTSStartedFrame())
    await collect_frames(
        processor, TTSAudioRawFrame(b"\x00\x01", sample_rate=24000, num_channels=1)
    )
    await collect_frames(processor, TTSTextFrame("Tokyo is currently ahead of UTC."))
    await collect_frames(processor, LLMFullResponseEndFrame())

    total_span = next(
        span for span in fake_tracer.spans if span.name == TOTAL_MOUTH_TO_EAR_SPAN_NAME
    )
    tts_span = next(span for span in fake_tracer.spans if span.name == TTS_STREAM_SPAN_NAME)
    assert total_span.ended
    assert tts_span.ended
    assert tts_span.attributes["voice.tts.provider"] == "deepgram"
    assert tts_span.attributes["voice.tts.voice_id"] == "aura-2-andromeda-en"
    assert isinstance(tts_span.attributes["voice.tts.first_chunk_ms"], int)
    assert tts_span.attributes["voice.tts.total_chars"] == len("Tokyo is currently ahead of UTC.")
