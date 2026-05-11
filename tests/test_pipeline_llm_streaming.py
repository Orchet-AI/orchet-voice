from __future__ import annotations

from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame

from tests.test_pipeline_helpers import FakeTracer, collect_frames
from voice.pipeline import LLM_STREAM_SPAN_NAME, LLMSpanProcessor, VoiceMetadata, VoiceTurnTracker


async def test_llm_stream_span_records_ttft_and_token_count(fake_tracer: FakeTracer) -> None:
    metadata = VoiceMetadata(
        voice_session_id="voice_test",
        user_id="user_test",
        llm_model="llama-3.3-70b-versatile",
    )
    tracker = VoiceTurnTracker(metadata)
    tracker.start_turn("turn_test")
    processor = LLMSpanProcessor(tracker, metadata)

    await collect_frames(processor, LLMFullResponseStartFrame())
    await collect_frames(processor, LLMTextFrame("Tokyo is nine hours ahead of UTC."))
    await collect_frames(processor, LLMFullResponseEndFrame())

    span = next(span for span in fake_tracer.spans if span.name == LLM_STREAM_SPAN_NAME)
    assert span.ended
    assert span.attributes["voice.llm.provider"] == "groq"
    assert span.attributes["voice.llm.model"] == "llama-3.3-70b-versatile"
    assert isinstance(span.attributes["voice.llm.ttft_ms"], int)
    tokens = span.attributes["voice.llm.total_tokens_out"]
    assert isinstance(tokens, int)
    assert tokens > 0
