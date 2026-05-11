from __future__ import annotations

from dataclasses import dataclass

from pipecat.frames.frames import AudioRawFrame, Frame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice.obs.tracing import get_tracer

ECHO_SPAN_NAME = "voice.echo.roundtrip"


@dataclass(frozen=True)
class EchoMetadata:
    voice_session_id: str
    client_kind: str = "web"


def echo_audio_frame(frame: AudioRawFrame) -> OutputAudioRawFrame:
    return OutputAudioRawFrame(
        audio=bytes(frame.audio),
        sample_rate=frame.sample_rate,
        num_channels=frame.num_channels,
    )


class EchoAudioProcessor(FrameProcessor):
    def __init__(self, metadata: EchoMetadata):
        super().__init__(name="orchet-echo-audio")
        self._metadata = metadata
        self._tracer = get_tracer()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, AudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            with self._tracer.start_as_current_span(ECHO_SPAN_NAME) as span:
                span.set_attribute("voice.session_id", self._metadata.voice_session_id)
                span.set_attribute("client.kind", self._metadata.client_kind)
                span.set_attribute("audio.sample_rate", frame.sample_rate)
                span.set_attribute("audio.num_channels", frame.num_channels)
                span.set_attribute("audio.bytes", len(frame.audio))
                await self.push_frame(echo_audio_frame(frame), FrameDirection.DOWNSTREAM)
            return

        await self.push_frame(frame, direction)
