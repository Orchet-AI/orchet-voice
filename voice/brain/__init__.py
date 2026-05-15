"""voice.brain — the read/write surface between voice and the user-memory layer.

Today (Phase 1 of the Brain-for-voice initiative — see
docs/strategy/BRAIN-FOR-VOICE.md in orchet-backend, governed by ADR-013)
this package holds only the session-start read path: a narrow MemoryPort
that returns a pre-rendered USER CONTEXT system-prompt block, plus its
HTTP adapter against orchet-backend's POST /voice/session-context.

Future phases add fact-write hooks (P2), preference-event hooks (P3),
knowledge-graph traversal helpers (P4), and proactive-context triggers
(P5). All of those will be additional ports + adapters under this
package — no consumer should reach past the port surface.

Design intent:
    - The port is a Protocol so test code can hand in a fake without
      monkeypatching httpx.
    - The HTTP adapter fails open. The base locale prompt still works
      with no context; a slow or unreachable backend simply produces a
      session that knows less about the user — never one that won't
      start.
"""

from voice.brain.memory_backend import (
    BrainMemoryAdapter,
    create_brain_memory_adapter,
)
from voice.brain.memory_port import (
    MemoryPort,
    SessionContext,
)

__all__ = [
    "MemoryPort",
    "SessionContext",
    "BrainMemoryAdapter",
    "create_brain_memory_adapter",
]
