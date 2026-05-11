from __future__ import annotations

import base64
import json
from typing import Any, cast

from pipecat.frames.frames import TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame

from voice.providers import tts_sarvam
from voice.providers.tts_sarvam import SarvamTTSService, parse_sarvam_tts_message


def test_sarvam_tts_config_matches_bulbul_v2_streaming_shape() -> None:
    service = SarvamTTSService(
        api_key="test-key",
        target_language_code="hi-IN",
        model="bulbul:v2",
        speaker="anushka",
        sample_rate=24000,
        output_audio_codec="linear16",
    )

    config = service.config_message()
    data = cast(dict[str, Any], config["data"])
    assert config["type"] == "config"
    assert data["model"] == "bulbul:v2"
    assert data["target_language_code"] == "hi-IN"
    assert data["speaker"] == "anushka"
    assert data["speech_sample_rate"] == "24000"
    assert data["output_audio_codec"] == "linear16"


def test_sarvam_tts_parse_audio_message() -> None:
    message = parse_sarvam_tts_message(
        json.dumps(
            {
                "type": "audio",
                "data": {
                    "content_type": "audio/wav",
                    "audio": base64.b64encode(b"\x01\x02").decode("ascii"),
                },
            }
        )
    )

    assert message.audio == b"\x01\x02"


async def test_sarvam_tts_provider_streams_mocked_ws(monkeypatch) -> None:
    sent_messages: list[dict[str, object]] = []
    audio_message = json.dumps(
        {
            "type": "audio",
            "data": {
                "content_type": "audio/wav",
                "audio": base64.b64encode(b"\x01\x02").decode("ascii"),
            },
        }
    )
    final_message = json.dumps({"type": "event", "data": {"event_type": "final"}})

    class FakeConnection:
        def __init__(self) -> None:
            self._messages = [audio_message, final_message]

        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def send(self, message: str) -> None:
            sent_messages.append(json.loads(message))

        def __aiter__(self) -> FakeConnection:
            return self

        async def __anext__(self) -> str:
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    def fake_connect(url: str, **kwargs: object) -> FakeConnection:
        assert "text-to-speech/ws" in url
        assert kwargs["extra_headers"] == {"api-subscription-key": "test-key"}
        return FakeConnection()

    monkeypatch.setattr(tts_sarvam.websockets, "connect", fake_connect)
    service = SarvamTTSService(
        api_key="test-key",
        target_language_code="hi-IN",
        model="bulbul:v2",
        speaker="anushka",
        sample_rate=24000,
        output_audio_codec="linear16",
    )
    service._sample_rate = 24000

    frames = [frame async for frame in service.run_tts("नमस्ते")]

    assert isinstance(frames[0], TTSStartedFrame)
    assert isinstance(frames[1], TTSAudioRawFrame)
    assert isinstance(frames[2], TTSStoppedFrame)
    assert sent_messages[0]["type"] == "config"
    assert sent_messages[1] == {"type": "text", "data": {"text": "नमस्ते"}}
    assert sent_messages[2] == {"type": "flush"}
