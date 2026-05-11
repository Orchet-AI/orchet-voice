from __future__ import annotations

from pipecat.frames.frames import TransportMessageUrgentFrame, TTSTextFrame

from tests.test_pipeline_helpers import collect_frames
from voice.pipeline import ClientVADInterruptionProcessor, VoiceMetadata, VoiceTurnTracker


async def test_confirmation_resolved_listener_pushes_tts_text() -> None:
    dispatcher = FakeDispatcher()
    processor = ClientVADInterruptionProcessor(
        VoiceTurnTracker(VoiceMetadata(voice_session_id="voice_test", user_id="user_test")),
        dispatcher,
    )

    pushed = await collect_frames(
        processor,
        TransportMessageUrgentFrame(
            {
                "type": "confirmation_resolved",
                "confirmation_id": "conf_test",
                "result": "executed",
                "voice_continuation_hint": "Done. Your flight is booked.",
            }
        ),
    )

    assert dispatcher.resolved == [("conf_test", "executed")]
    assert len(pushed) == 1
    assert isinstance(pushed[0][0], TTSTextFrame)
    assert pushed[0][0].text == "Done. Your flight is booked."


class FakeDispatcher:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str]] = []

    async def snapshot_interrupted(self, snapshot: dict[str, object]) -> None:
        raise AssertionError(f"unexpected snapshot: {snapshot!r}")

    def resolve_confirmation(self, confirmation_id: str, result: str) -> None:
        self.resolved.append((confirmation_id, result))
