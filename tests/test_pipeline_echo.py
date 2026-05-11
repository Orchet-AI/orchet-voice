from __future__ import annotations

from pipecat.frames.frames import AudioRawFrame, OutputAudioRawFrame

from voice.pipeline import ECHO_SPAN_NAME, echo_audio_frame


def test_echo_audio_frame_preserves_pcm_bytes() -> None:
    frame = AudioRawFrame(audio=b"\x00\x01\x02\x03", sample_rate=16000, num_channels=1)

    echoed = echo_audio_frame(frame)

    assert isinstance(echoed, OutputAudioRawFrame)
    assert echoed.audio == frame.audio
    assert echoed.sample_rate == 16000
    assert echoed.num_channels == 1


def test_echo_span_name_is_load_bearing() -> None:
    assert ECHO_SPAN_NAME == "voice.echo.roundtrip"
