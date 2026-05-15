"""MemoryPort — the narrow read surface voice uses to enrich its system prompt.

Phase 1 deliberately keeps the port at a single method. The reason is
discipline: every additional method becomes a contract the backend must
satisfy AND a path that has to be mocked in voice unit tests AND a
permutation a future migration must preserve. We expand only when a new
use case can't be served by the existing shape.

The port is *transport-agnostic*. Today the only adapter is
BackendMemoryAdapter (HTTP to orchet-backend /voice/session-context),
but the port can be served just as well by:

  - an in-process fake for tests
  - a direct Supabase read (if voice ever needs DB locality)
  - a multi-region cache layer in front of the backend

Companion ADR: orchet-backend/docs/architecture/decisions/013-brain-for-voice-memory-topology.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SessionContext:
    """The pre-rendered slice of memory the voice service needs at
    session start.

    Fields mirror the backend POST /voice/session-context response
    one-for-one — keep them aligned. Any new field on either side is
    a deliberate contract change requiring both PRs to land together.
    """

    # The ready-to-append "USER CONTEXT" block. None when the user has
    # neither a profile nor any facts yet — voice falls back to the
    # base locale prompt unchanged in that case.
    system_message: str | None

    # Telemetry — true when at least one profile field was rendered.
    profile_loaded: bool

    # Telemetry — count of facts included in the block.
    facts_count: int

    # Server-side elapsed milliseconds. Already includes the Supabase
    # round-trip + compose. Voice records its own client-side elapsed
    # separately for total budget tracking.
    elapsed_ms: int

    # Set when the backend's compose budget timed out and it returned
    # a best-effort partial result. Voice still uses the partial; this
    # is informational so we can spot pgvector tail latency.
    partial: bool = False

    @property
    def has_content(self) -> bool:
        """True when this context carries anything worth injecting.

        Used as the gate for prompt-append: never inject empty
        USER CONTEXT blocks (they'd just add noise tokens with no
        signal).
        """
        return self.system_message is not None and bool(self.system_message.strip())


class MemoryPort(Protocol):
    """Read the session-start memory slice for a user.

    Implementations MUST be safe to call with arbitrary user_id values
    — invalid / anonymous / unknown ids should resolve to an empty
    SessionContext, not raise. The voice pipeline can't usefully react
    to a memory exception; it always wants a coherent shape back.

    Implementations SHOULD honor the timeout budget. The default
    contract from the backend is 400 ms server-side compose; voice
    caps the wall-clock at 500 ms. Adapters slower than that should
    return a partial result rather than block the pipeline.
    """

    async def get_session_context(
        self,
        *,
        user_id: str,
        voice_session_id: str | None = None,
        agent_id: str | None = None,
        locale: str | None = None,
    ) -> SessionContext: ...
