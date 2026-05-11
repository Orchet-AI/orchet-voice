# Phase 0 — Measurement plan

**Status:** Approved, ready to execute
**Owner:** TBD (Codex / contractor / Kalas)
**Duration:** 3 days
**Goal:** Establish a defensible p50 / p95 / p99 latency baseline for the current REST-based voice path. Every later phase will compare against these numbers to prove improvement.

---

## Why we measure before we build

Three reasons:

1. **Prove the new architecture's improvement.** If we build the Fly.io + Pipecat + WebRTC stack and ship it without baseline numbers, we can't tell stakeholders "this is N× faster than what we had." Numbers convince; impressions don't.
2. **Identify the dominant cost in the current path.** The new architecture will eliminate some hops but may not be the win we expect on others. Knowing where the time goes prevents over-engineering.
3. **Catch easy wins on the current path while the new stack is being built.** If we discover the existing TTS round-trip is 1.4 s due to bad client buffering, we can fix that in one PR without waiting for the full v5 rollout.

---

## The seven spans (canonical list)

Instrument the existing voice flow with the following OpenTelemetry spans. Names are fixed — downstream dashboards and queries depend on these exact strings.

| Span | Start | End | Owner |
|---|---|---|---|
| `voice.client.capture` | User taps mic | First audio chunk encoded + ready to upload | Web/iOS client |
| `voice.upload` | First chunk sent | Upload complete (HTTP 200 from `/stt`) | Network / client |
| `voice.stt.batch` | Integrations `/stt` route enters | Deepgram REST returns transcript | `services/integrations/src/routes/stt.ts` |
| `voice.orchestrator.turn` | Orchestrator `/turn` route enters | Final SSE frame `{type:"done"}` emitted | `services/orchestrator/src/routes/turn.ts` |
| `voice.tts.batch` | Integrations `/tts` route enters | Deepgram REST returns audio + 200 response | `services/integrations/src/routes/tts.ts` |
| `voice.client.play` | Audio response received by client | First audible sample played to speakers | Web/iOS client |
| `voice.total.mouth_to_ear` | User finishes speaking (final transcript) | First audible response sample | Aggregate span — parent of the others |

All spans use the existing OpenTelemetry tracer wired through `@orchet/observability`. Honeycomb is the destination; the API key is already in the backend's Render env (`ORCHET_HONEYCOMB_API_KEY`).

---

## Where instrumentation lives

**Web client** (`Orchet-AI/orchet-web/app/voice-mode/*`):
- `voice.client.capture` — wrap `MediaRecorder.start()` → `onstop` with a span
- `voice.upload` — wrap the `fetch('/api/stt', ...)` call
- `voice.client.play` — wrap from `<audio>` element's `oncanplay` event to `onplaying`
- `voice.total.mouth_to_ear` — outer span from "user finished speaking" (VAD endpointing) to "first audio sample played"

**iOS client** (`Orchet-AI/orchet-ios/Lumo/Services/{ChatService,VoiceMode}.swift`):
- Same four spans, instrumented via `OSSignposter` → OTel exporter
- Export to the same Honeycomb dataset

**Backend** (`Orchet-AI/orchet-backend/services/{integrations,orchestrator}`):
- `voice.stt.batch` — wrap `deepgramListenRestUrl` call in `services/integrations/src/routes/stt.ts`
- `voice.tts.batch` — wrap `deepgramSpeakRestUrl` call in `services/integrations/src/routes/tts.ts`
- `voice.orchestrator.turn` — wrap `runTurn` call in `services/orchestrator/src/routes/turn.ts`

**Trace propagation:** the gateway already forwards `traceparent` headers per W3C Trace Context. Each backend span is a child of the client's `voice.total.mouth_to_ear` span. No new propagation work required.

---

## What we report

A 1-page Honeycomb dashboard with five panels:

1. **End-to-end** — `voice.total.mouth_to_ear` p50 / p95 / p99 over last 24h, broken down by client (web / iOS).
2. **Per-stage** — bar chart of p50 for each of the six child spans, side by side.
3. **Tail latency** — p95 / p99 for each stage; identifies which stage is the long-tail offender.
4. **Geography** — same p50 broken down by client IP region (US / EU / India / SEA).
5. **Provider** — STT and TTS latency p50 over time; flags Deepgram regressions.

---

## What "done" looks like

- All seven spans emitting to Honeycomb in staging and prod
- Dashboard live and shared with the team
- A short markdown report (`docs/phase-0-baseline.md`) checked in with the actual numbers and three observations:
  - The dominant stage by p50 latency
  - The worst stage by p99 / p50 ratio (tail latency offender)
  - Any region with materially worse experience

---

## Out of scope for Phase 0

- Changing any production code paths
- Improving any of the current latencies (that's Phases 1–5)
- Sarvam integration (separate doc; runs in parallel)
- Touching iOS production voice flow
- Any WebRTC work

---

## Open questions

1. **Are existing OpenTelemetry spans in the voice path already emitting?** Quick audit needed before adding new ones — we may already have 3 of the 6 child spans and just need the orchestration parent.
2. **Should we sample at 100% in Phase 0?** Current voice traffic is low enough that 100% is fine and gives us complete tail data. Default to that unless ops disagrees.
3. **Does iOS app currently emit OTel?** If not, this requires the `swift-otel` SDK and a small bootstrap; flag if iOS instrumentation is a separate ~2-day task.

---

## Phase 1 readiness gate

This document is complete. Phase 0 is ready to be dispatched to engineering. Once the dashboard is live and the baseline report is committed, Phase 1 (orchet-voice repo skeleton + Fly.io echo round-trip) can start.
