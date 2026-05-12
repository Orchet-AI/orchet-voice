from __future__ import annotations

import time

from pipecat.frames.frames import (
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.transcriptions.language import Language

from tests.test_pipeline_helpers import collect_frames
from voice.pipeline import VoiceMetadata, VoiceTurnTracker
from voice.routing.language_router import (
    AsyncLanguageDetector,
    LanguageDetectionProcessor,
    LanguageDetectionResult,
)


class FakeDetector:
    def __init__(self, locale: str = "hi-IN"):
        self.locale = locale
        self.calls = 0

    async def detect(
        self,
        audio_first_seconds: bytes,
        *,
        sample_rate: int,
    ) -> LanguageDetectionResult:
        del audio_first_seconds, sample_rate
        self.calls += 1
        return LanguageDetectionResult(
            locale=self.locale,
            confidence=0.91,
            provider="fake-detector",
            elapsed_ms=12,
        )


async def test_deepgram_high_confidence_english_at_300ms_locks_deepgram() -> None:
    detector = FakeDetector()
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    await collect_frames(processor, _audio_frame(seconds=0.2))
    pushed = await collect_frames(
        processor,
        _interim("hello there", language=Language.EN_US, confidence=0.92),
    )

    assert detector.calls == 0
    assert tracker.locale == "en-US"
    assert tracker.stt_provider == "deepgram"
    assert tracker.tts_provider == "deepgram"
    assert any(isinstance(frame, UserStartedSpeakingFrame) for frame, _ in pushed)
    assert any(isinstance(frame, InputAudioRawFrame) for frame, _ in pushed)


async def test_deepgram_low_confidence_at_300ms_falls_through_to_detector() -> None:
    detector = FakeDetector(locale="hi-IN")
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    await collect_frames(processor, _audio_frame(seconds=0.2))
    await collect_frames(processor, _interim("hello", language=Language.EN_US, confidence=0.4))
    await collect_frames(processor, _audio_frame(seconds=2.0))

    assert detector.calls == 1
    assert tracker.locale == "hi-IN"
    assert tracker.stt_provider == "sarvam"
    assert tracker.tts_provider == "sarvam"


async def test_high_confidence_non_english_does_not_short_circuit() -> None:
    detector = FakeDetector(locale="te-IN")
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    await collect_frames(processor, _audio_frame(seconds=0.2))
    await collect_frames(processor, _interim("namaste", language=Language.HI_IN, confidence=0.95))
    await collect_frames(processor, _audio_frame(seconds=2.0))

    assert detector.calls == 1
    assert tracker.locale == "te-IN"
    assert tracker.stt_provider == "sarvam"


async def test_no_interim_within_500ms_falls_through_to_detector() -> None:
    detector = FakeDetector(locale="ta-IN")
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    processor._speech_started_at = time.perf_counter() - 0.6  # noqa: SLF001
    await collect_frames(
        processor, _interim("late hello", language=Language.EN_US, confidence=0.95)
    )
    await collect_frames(processor, _audio_frame(seconds=2.0))

    assert detector.calls == 1
    assert tracker.locale == "ta-IN"
    assert tracker.stt_provider == "sarvam"


class RaisingDetector:
    """Simulates Sarvam streaming detector timing out or erroring mid-call."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def detect(
        self,
        audio_first_seconds: bytes,
        *,
        sample_rate: int,
    ) -> LanguageDetectionResult:
        del audio_first_seconds, sample_rate
        self.calls += 1
        raise self._exc


async def test_detector_timeout_does_not_crash_pipeline_falls_back_to_english() -> None:
    """Regression test for the 2026-05-13 bom-Fly production incident:
    when SarvamStreamingLanguageDetector.detect() raised TimeoutError
    out of asyncio.wait_for, the exception propagated through
    LanguageDetectionProcessor._detect_and_release and crashed the
    entire Pipecat pipeline. The bot would join the Daily room, hit
    the language-detection step on the first speech turn, raise, and
    the pipeline would die mid-turn — leaving the room with a zombie
    bot that couldn't process any further audio. The user saw 'listening'
    indefinitely with no response."""
    detector = RaisingDetector(TimeoutError())
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    # Drive enough audio to trigger _detect_and_release
    await collect_frames(processor, _audio_frame(seconds=2.1))

    assert detector.calls == 1
    # Fallback: locale defaults to "en" which normalize_locale lifts to "en-US" → Deepgram path
    assert tracker.locale == "en-US"
    assert tracker.stt_provider == "deepgram"
    assert tracker.tts_provider == "deepgram"


async def test_detector_generic_exception_does_not_crash_pipeline() -> None:
    """Same regression — any unexpected exception from the detector
    should fall back to English/Deepgram instead of taking the bot down."""
    detector = RaisingDetector(RuntimeError("sarvam blew up"))
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    await collect_frames(processor, _audio_frame(seconds=2.1))

    assert detector.calls == 1
    assert tracker.locale == "en-US"
    assert tracker.stt_provider == "deepgram"


async def test_interim_without_confidence_uses_deepgram_english_fallback() -> None:
    detector = FakeDetector()
    tracker = VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test"))
    processor = _processor(tracker, detector)

    await collect_frames(processor, UserStartedSpeakingFrame())
    await collect_frames(processor, _interim("hello without confidence"))

    assert detector.calls == 0
    assert tracker.locale == "en-US"
    assert tracker.stt_provider == "deepgram"


def _processor(
    tracker: VoiceTurnTracker,
    detector: AsyncLanguageDetector,
) -> LanguageDetectionProcessor:
    return LanguageDetectionProcessor(
        tracker=tracker,
        detector=detector,
        sarvam_tts_speaker="aditya",
        deepgram_tts_voice="aura-2-andromeda-en",
        detection_seconds=2.0,
    )


def _audio_frame(*, seconds: float) -> InputAudioRawFrame:
    sample_rate = 16000
    num_samples = int(sample_rate * seconds)
    return InputAudioRawFrame(b"\x00\x00" * num_samples, sample_rate=sample_rate, num_channels=1)


def _interim(
    text: str,
    *,
    language: Language | None = None,
    confidence: float | None = None,
) -> InterimTranscriptionFrame:
    frame = InterimTranscriptionFrame(text, "user", "ts", language)
    if confidence is not None:
        frame.metadata["confidence"] = confidence
    return frame
