from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Status
from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


@dataclass
class FakeSpan:
    name: str
    attributes: dict[str, object] = field(default_factory=dict)
    ended: bool = False
    status: Status | None = None

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status: Status) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True

    def is_recording(self) -> bool:
        return not self.ended


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_span(self, name: str, context: Any | None = None) -> FakeSpan:
        span = FakeSpan(name=name)
        self.spans.append(span)
        return span


async def collect_frames(
    processor: FrameProcessor,
    frame: Frame,
    direction: FrameDirection = FrameDirection.DOWNSTREAM,
) -> list[tuple[Frame, FrameDirection]]:
    pushed: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(
        pushed_frame: Frame,
        pushed_direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        pushed.append((pushed_frame, pushed_direction))

    processor.push_frame = push_frame  # type: ignore[method-assign]
    await processor.process_frame(frame, direction)
    return pushed
