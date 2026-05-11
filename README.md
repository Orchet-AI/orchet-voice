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

| Phase | Status | Brief / Result |
|---|---|---|
| 0 — Measurement + Sarvam eval | ✅ Closed (4 PRs merged across orchet-backend/web/ios/voice) | [Baseline](docs/phase-0-baseline.md), [Runbook](docs/phase-0-runbook.md) |
| 1 — orchet-voice skeleton + Fly.io echo + India probe | ⏳ Dispatched to Codex (issue [#4](https://github.com/Orchet-AI/orchet-voice/issues/4)) | [Phase 1 brief](docs/briefs/VOICE-PHASE-1-CODEX-BRIEF.md) |
| 2 — Streaming STT + LLM + TTS + barge-in | 📝 Brief ready; dispatches after Phase 1 | [Phase 2 brief](docs/briefs/VOICE-PHASE-2-CODEX-BRIEF.md) |
| 3 — Orchestrator integration + visual confirmation | 📝 Brief ready; dispatches after Phase 2 | [Phase 3 brief](docs/briefs/VOICE-PHASE-3-CODEX-BRIEF.md) |
| 4 — Sarvam Indian-language layer | 📝 Brief ready; dispatches after Phase 3 + Sarvam evaluation Pass | [Phase 4 brief](docs/briefs/VOICE-PHASE-4-CODEX-BRIEF.md) |
| 5 — Multi-region + per-agent LLM router | 📝 Brief ready; dispatches after Phase 4 | [Phase 5 brief](docs/briefs/VOICE-PHASE-5-CODEX-BRIEF.md) |
| 6 — iOS WebRTC + production hardening | Not briefed yet — depends on Phase 5 production data | — |

## Infrastructure status (Phase 1 readiness)

| Resource | Status |
|---|---|
| Fly.io org `orchet` + payment unlocked | ✅ |
| Fly app `orchet-voice` (id `p7vx1jjdrx3n1k3z`) | ✅ pending (no machines yet — Codex deploys the first one) |
| Fly org token `FLY_API_TOKEN` (expires 2027-05-11) | ✅ saved in CLAUDE-MEMORY |
| **14 Fly secrets pre-set** | ✅ ANTHROPIC_API_KEY, DAILY_API_KEY, DAILY_ROOM_DOMAIN, GROQ_API_KEY, LUMO_DEEPGRAM_API_KEY, LUMO_OTEL_ENDPOINT, LUMO_OTEL_HEADERS, NEXT_PUBLIC_SUPABASE_ANON_KEY, NEXT_PUBLIC_SUPABASE_URL, ORCHET_GATEWAY_URL, ORCHET_HONEYCOMB_API_KEY, ORCHET_INTERNAL_TOKEN, ORCHET_VOICE_ENV, ORCHET_VOICE_LLM_DEFAULT |
| Daily.co account + subdomain `orchet.daily.co` | ✅ |
| Honeycomb dashboard (board `kvRUiNXYcvk`, 5 panels) | ✅ |
| Phase 0 voice spans live in production code | ✅ orchet-backend / orchet-web / orchet-ios |

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
