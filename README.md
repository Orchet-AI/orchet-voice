# orchet-voice

Real-time WebRTC voice agent service for Orchet. Long-lived Pipecat orchestration on Fly.io. Per **VOICE-ARCHITECTURE-1 v5**.

**Status:** Phase 0 — docs only. No production code yet.

## What this service is

A dedicated, always-on service that terminates WebRTC voice sessions from web and iOS clients, runs a streaming STT → LLM → TTS pipeline, and round-trips tool calls to `api.orchet.ai/voice/turn` (where the orchestrator owns policy and execution).

```
web / iOS mic
  → WebRTC (Daily transport)
  → orchet-voice (Fly.io, Pipecat)
  → Deepgram / Sarvam STT
  → Groq / Claude LLM router
  → Deepgram Aura-2 / Sarvam Bulbul TTS
  → backend via /voice/turn (tool decisions only)
  → orchestrator decides execute / visual-confirm / denied
```

## What this service is NOT

- NOT a phone-call / telephony service (PSTN out of scope; separate ADR if pursued)
- NOT a tool executor (orchestrator owns tool policy + execution)
- NOT a chat orchestrator (text chat path is unchanged)

## Phase status

| Phase | Status | Doc |
|---|---|---|
| 0 — Measurement + Sarvam eval | Phase 0 plan committed | [phase-0-measurement-plan.md](docs/phase-0-measurement-plan.md), [sarvam-evaluation-plan.md](docs/sarvam-evaluation-plan.md) |
| 1 — Skeleton + India probe | Plan committed | [repo-scaffold-plan.md](docs/repo-scaffold-plan.md) |
| 2 — Streaming pipeline + interruption | Not started | — |
| 3 — Backend orchestration + visual confirmation | Not started | [voice-turn-contract-proposal.md](docs/voice-turn-contract-proposal.md) |
| 4 — Sarvam Indian-language layer | Not started | — |
| 5 — Multi-region | Not started | — |
| 6 — Production hardening | Not started | — |

## Architecture

The authoritative architecture document is **[docs/architecture/VOICE-ARCHITECTURE-1.md](docs/architecture/VOICE-ARCHITECTURE-1.md)** (v5, approved 2026-05-10).

Hard rules (re-stated):
1. Voice service does NOT execute tools
2. Backend orchestrator owns all tool policy, memory, permissions, confirmation, execution, audit
3. Irreversible actions ALWAYS require visual confirmation
4. Telephony is out of scope
5. Voice channel = browser + iOS WebRTC ONLY
6. Voice requires authenticated users (no anonymous voice)

## Quick links

- [Architecture (VOICE-ARCHITECTURE-1)](docs/architecture/VOICE-ARCHITECTURE-1.md)
- [Phase 0 — Measurement plan](docs/phase-0-measurement-plan.md)
- [Phase 0 — Sarvam evaluation plan](docs/sarvam-evaluation-plan.md)
- [Voice-turn contract proposal](docs/voice-turn-contract-proposal.md) (lands in Phase 3)
- [Repo scaffold plan](docs/repo-scaffold-plan.md) (lands in Phase 1)
