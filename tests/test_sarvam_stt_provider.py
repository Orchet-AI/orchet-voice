from __future__ import annotations

import json

from voice.providers.stt_sarvam import (
    SarvamSTTService,
    build_sarvam_stt_ws_url,
    parse_sarvam_stt_message,
)
from voice.routing import language_router
from voice.routing.language_router import SarvamStreamingLanguageDetector


def test_sarvam_stt_provider_builds_streaming_url() -> None:
    service = SarvamSTTService(
        api_key="test-key",
        target_language_code="te-IN",
        model="saarika:v2.5",
        sample_rate=16000,
    )

    assert service.target_language_code == "te-IN"
    assert "language-code=te-IN" in service.websocket_url
    assert "model=saarika%3Av2.5" in service.websocket_url
    assert "input_audio_codec=pcm_s16le" in service.websocket_url


def test_sarvam_stt_parse_transcription_message() -> None:
    message = parse_sarvam_stt_message(
        json.dumps(
            {
                "type": "data",
                "data": {
                    "transcript": "నమస్తే",
                    "language_code": "te-IN",
                    "language_probability": 0.93,
                    "metrics": {"audio_duration": 1.0, "processing_latency": 0.1},
                },
            }
        )
    )

    assert message.transcript == "నమస్తే"
    assert message.language_code == "te-IN"
    assert message.language_probability == 0.93


async def test_sarvam_language_detector_uses_mocked_ws(monkeypatch) -> None:
    sent_messages: list[str] = []

    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send(self, message: str) -> None:
            sent_messages.append(message)

        async def recv(self) -> str:
            return json.dumps(
                {
                    "type": "data",
                    "data": {
                        "transcript": "नमस्ते",
                        "language_code": "hi-IN",
                        "language_probability": 0.88,
                        "metrics": {"audio_duration": 1.0, "processing_latency": 0.1},
                    },
                }
            )

    def fake_connect(url: str, **kwargs: object) -> FakeConnection:
        assert url == build_sarvam_stt_ws_url(
            language_code="unknown",
            model="saarika:v2.5",
            sample_rate=16000,
            input_audio_codec="pcm_s16le",
            high_vad_sensitivity=True,
        )
        assert kwargs["extra_headers"] == {"api-subscription-key": "test-key"}
        return FakeConnection()

    monkeypatch.setattr(language_router.websockets, "connect", fake_connect)

    result = await SarvamStreamingLanguageDetector(api_key="test-key").detect(
        b"\x00" * 32000,
        sample_rate=16000,
    )

    assert result.locale == "hi-IN"
    assert result.confidence == 0.88
    assert result.provider == "sarvam-unknown"
    assert json.loads(sent_messages[0])["audio"]["sample_rate"] == "16000"
    assert json.loads(sent_messages[1]) == {"type": "flush"}
