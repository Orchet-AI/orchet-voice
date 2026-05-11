# Codex brief — VOICE-PHASE-0: instrument current REST voice path

**Brief ID:** VOICE-PHASE-0-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Status:** Dispatched (2026-05-10)
**Owner:** Codex
**Reviewer:** Kalas (CEO/CTO) + Claude
**Estimated effort:** 3 days

This brief is self-contained. Read the ADR (1 link above) for the strategic context, but you can execute everything below without re-deriving any decisions.

---

## Goal

Instrument the **existing** REST-based voice path with OpenTelemetry spans so we have a defensible p50 / p95 / p99 latency baseline before the new WebRTC + Pipecat stack is built. Every future phase compares against these numbers.

You are NOT building the new voice service. You are NOT touching the voice production code paths. You are adding measurement infrastructure to an existing path that already works.

---

## Hard scope boundaries

**You MUST NOT:**
- Build any part of the new Pipecat / WebRTC / Fly.io voice service (`Orchet-AI/orchet-voice` voice/ directory stays empty for now — only docs commits)
- Change tool execution policy or wiring
- Change payment, booking, or any irreversible-action flow
- Add telephony / PSTN / Exotel / μ-law audio handling
- Change the existing STT route, TTS route, or `/orchestrator/turn` request/response shapes (canonical: `/integrations/stt` / `/integrations/tts` via gateway; web/iOS callers may call a thin wrapper — do not invent `/api/*` paths)
- Introduce new STT, TTS, or LLM providers (Sarvam goes in Phase 3 — Phase 0 smoke harness only)
- Modify production user voice flows beyond adding spans (no UI changes, no latency optimizations, no refactors)
- Commit secrets, real user audio samples, or provider API keys

**You MUST:**
- Add the 7 named spans to the existing voice path (names are fixed — see below)
- Make sure existing tests stay green in every touched repo
- Make sure typechecks pass where you touched TypeScript / Swift
- Sample at 100% in staging; default to existing sampling rate in production
- Write the baseline report in markdown
- Optionally add the Sarvam smoke harness if you can do it safely (see "Sarvam smoke" section below — skip if any blocker)

---

## Deliverables

### 1. Seven OpenTelemetry spans, names exactly as below

| Span | Where it lives | Start trigger | End trigger |
|---|---|---|---|
| `voice.client.capture` | orchet-web + orchet-ios (client) | User taps mic | First audio chunk encoded + ready to upload |
| `voice.upload` | orchet-web + orchet-ios (client) | First chunk sent | Upload complete (HTTP 200 from the existing STT route) |
| `voice.stt.batch` | orchet-backend (`services/integrations/src/routes/stt.ts`) | Route enters | Deepgram REST returns transcript |
| `voice.orchestrator.turn` | orchet-backend (`services/orchestrator/src/routes/turn.ts`) | Route enters | Final SSE frame `{type:"done"}` emitted |
| `voice.tts.batch` | orchet-backend (`services/integrations/src/routes/tts.ts`) | Route enters | Deepgram REST returns audio + 200 |
| `voice.client.play` | orchet-web + orchet-ios (client) | Audio response received | First audible sample played |
| `voice.total.mouth_to_ear` | client (parent span) | User finishes speaking (VAD endpointing or transcript final) | First audible response sample |

**Span names are load-bearing.** Honeycomb dashboards and downstream telemetry queries will reference these exact strings. Do not abbreviate, do not pluralize, do not change case.

**Parent/child relationships (pragmatic, not perfectionist):**
- `voice.total.mouth_to_ear` is the **client-side parent span where possible** — covers `voice.client.capture`, `voice.upload`, `voice.client.play` on the client
- Backend spans (`voice.stt.batch`, `voice.orchestrator.turn`, `voice.tts.batch`) should be **correlated through W3C trace context (`traceparent`) when propagation is already working** — the gateway forwards `traceparent` per the existing `@orchet/observability` setup, so if web/iOS already emit `traceparent` on outbound requests, backend spans nest automatically
- **If `traceparent` propagation is not already wired on web or iOS, do NOT add it as part of this brief.** Instead, correlate by stamping all spans (client + backend) with attributes:
  - `voice.session_id` — same per voice turn
  - `voice.turn_id` — unique per turn
  - `client.kind` — `web` / `ios`
- A Honeycomb query joining on `voice.turn_id` is good enough for Phase 0. Perfect distributed tracing is Phase 6 production-hardening work, not Phase 0 measurement work.

### 2. Repos to touch

| Repo | What | Why |
|---|---|---|
| `Orchet-AI/orchet-web` | Add client-side spans in voice mode component(s); search for `MediaRecorder`, the existing STT-upload `fetch(...)` call, and audio playback handlers. Likely under `app/voice-mode/` or wherever the voice UI lives. **Do not invent `/api/*` paths — find and use whatever path the existing code calls.** | Web users are the primary measurement target for Phase 0 |
| `Orchet-AI/orchet-backend` | Wrap the three named routes with spans; reuse existing `@orchet/observability` tracer | Server-side is where the slowest hops live |
| `Orchet-AI/orchet-ios` | Add same client spans via `OSSignposter` (+ structured timing logs stamped with `voice.session_id` / `voice.turn_id`). Only wire a full OTel exporter if one already exists or the integration is < 2 hours. iOS voice flow lives in `Lumo/Services/{ChatService.swift, CompoundStreamService.swift}` and related files. | iOS users may have a materially different latency profile — measure separately, but don't blow up scope adding a new SDK |
| `Orchet-AI/orchet-voice` | **Docs only** — commit `docs/phase-0-baseline.md` with the baseline numbers + observations | Phase 0 is docs-only on the voice repo |

### 3. Honeycomb dashboard or dashboard spec (1 page, 5 panels)

**Conditional execution:** If you have access to the Orchet Honeycomb account, create the dashboard. (API key exists as `ORCHET_HONEYCOMB_API_KEY` in Render env for orchet-backend; web UI access is a separate credential.)

**If you do NOT have Honeycomb web UI access:** commit a `docs/phase-0-honeycomb-dashboard-spec.yaml` (or `.md`) describing each panel's query, group-by, percentile, time window, and visualization type. Leave the dashboard URL in `docs/phase-0-baseline.md` as `TODO — create dashboard from spec` and flag in your status report that a human with Honeycomb access needs to materialize it.

Panels:

1. **End-to-end** — `voice.total.mouth_to_ear` p50 / p95 / p99 over last 24h, grouped by `client.kind` (web / ios)
2. **Per-stage** — bar chart: p50 of each child span side-by-side
3. **Tail latency** — p99 / p50 ratio per stage; identifies which stage has the worst tail
4. **Geography** — `voice.total.mouth_to_ear` p50 grouped by `client.ip.region` (US / EU / India / SEA / other)
5. **Provider** — `voice.stt.batch` and `voice.tts.batch` p50 over time; flags provider regressions

Dashboard link gets recorded in the baseline report.

### 4. Baseline report — `Orchet-AI/orchet-voice/docs/phase-0-baseline.md`

Markdown file with this exact section structure:

```
# Phase 0 — Voice baseline

**Measured:** YYYY-MM-DD
**Dashboard:** <Honeycomb URL>
**Traffic window:** <timestamp range>
**Sample count:** N web + N ios turns

## Headline numbers

| Stage | p50 | p95 | p99 |
|---|---|---|---|
| voice.total.mouth_to_ear | ... | ... | ... |
| voice.client.capture | ... | ... | ... |
| voice.upload | ... | ... | ... |
| voice.stt.batch | ... | ... | ... |
| voice.orchestrator.turn | ... | ... | ... |
| voice.tts.batch | ... | ... | ... |
| voice.client.play | ... | ... | ... |

## By geography

| Region | total p50 | total p95 | sample count |
|---|---|---|---|
| US | ... | ... | ... |
| EU | ... | ... | ... |
| India / SEA | ... | ... | ... |

## Three observations

1. **Dominant stage by p50 latency** — <which stage costs the most time and why>
2. **Worst stage by p99 / p50 ratio** — <which stage has the longest tail and why>
3. **Worst region** — <which geography is materially worse and what's the gap>

## What this tells us about the new architecture targets

- ...

## Caveats

- ...
```

If live traffic is too low to fill in real numbers in the time window: use a placeholder table + step-by-step instructions for how to re-run the report once traffic accumulates.

### 5. Documentation — how to run the measurement

Add `Orchet-AI/orchet-voice/docs/phase-0-runbook.md` that documents:

- How to trigger a test voice turn from a dev machine (curl-based or browser-based)
- How to verify spans are emitted to Honeycomb (filter `service.name = orchet-backend` AND `span.name starts-with voice.`)
- How to refresh the baseline report from the dashboard
- Known limitations (e.g. if sampling drops at high traffic)

### 6. (Optional) Sarvam smoke harness

**Only do this if you can build it without:**
- Touching production code paths
- Adding live Sarvam API calls to user-facing routes
- Committing real Sarvam API keys (use a placeholder + a "BYO key" runbook step)

If safe to add, commit at `Orchet-AI/orchet-voice/tests/sarvam-smoke/`:

- A simple Python script that takes audio samples + a `SARVAM_API_KEY` env var, calls Sarvam Saarika STT + Bulbul TTS, records first-partial latency, final-transcript latency, first-chunk TTS latency
- Sample audio for Hindi, Telugu, Tamil + 3 Hinglish code-mixed sentences (script can be the script — words to read, NOT audio files — record locally)
- A README explaining how to run it and how to interpret results
- Output: `docs/sarvam-evaluation-result.md` template (filled in once a human runs the harness)

If any blocker (audio sample sourcing, Sarvam signup, etc.) — skip the harness, commit only the runbook + scoring template.

---

## Execution order (recommended)

1. **Day 1 morning** — read [`VOICE-ARCHITECTURE-1.md`](../architecture/VOICE-ARCHITECTURE-1.md) + [`phase-0-measurement-plan.md`](../phase-0-measurement-plan.md). Grep the three target repos for `voice.`, `MediaRecorder`, `Deepgram`, `OSSignposter` to map current call sites.
2. **Day 1 afternoon** — instrument `orchet-backend` (smallest blast radius, easiest to verify). Three spans: `voice.stt.batch`, `voice.orchestrator.turn`, `voice.tts.batch`. PR to orchet-backend.
3. **Day 2 morning** — instrument `orchet-web` voice mode. Four spans including the parent `voice.total.mouth_to_ear`. PR to orchet-web.
4. **Day 2 afternoon** — instrument `orchet-ios` voice mode. **Pragmatic order:**
   - First check: does the iOS app already emit OpenTelemetry? If yes, use the existing setup
   - If no: use `OSSignposter` for the four named spans (signpost intervals named exactly per the table) + structured timing logs stamped with `voice.session_id` and `voice.turn_id`
   - **Do NOT add a full `swift-otel` SDK dependency as part of this brief** — that's a multi-day dependency-management task on its own and would explode Phase 0 scope. Stop and report if iOS OTel-SDK setup looks like more than 2 hours of work.
   PR to orchet-ios.
5. **Day 3 morning** — Honeycomb dashboard setup. Trigger ~50 test turns from web + iOS to verify trace continuity. Capture screenshots of the dashboard.
6. **Day 3 afternoon** — write the baseline report (real numbers if traffic permits, else placeholder + runbook). Write the runbook. Optionally add Sarvam harness. PR to orchet-voice.

---

## Verification checklist

Before opening any PR:

- [ ] `npm run typecheck` (or `pnpm` / `bun` equivalent) passes in every touched workspace
- [ ] `npm run test` passes in every touched workspace
- [ ] iOS: `xcodebuild build` for the Orchet target compiles without warnings introduced
- [ ] No secrets in diff (`git diff main | grep -E "(sk-|api_key|API_KEY|SECRET|TOKEN|password|pat_)"`)
- [ ] No real user audio files committed
- [ ] No provider keys logged (audit `console.log`, `logger.info`, `print` etc near new instrumentation)
- [ ] Spans use exact names from the table above (no abbreviation / no rename)
- [ ] Each span has appropriate attributes set (at minimum: `user.id` hashed or omitted per PII policy, `agent.id`, `session.id`, `client.kind`, `region` where applicable)
- [ ] `voice.total.mouth_to_ear` is correlated with backend spans either through `traceparent` propagation or through `voice.session_id` + `voice.turn_id` attributes (whichever is already available — adding `traceparent` propagation is out of scope for this brief)

After the PRs land but before reporting completion:

- [ ] Honeycomb dashboard URL recorded in `docs/phase-0-baseline.md`
- [ ] At least one end-to-end trace visible in Honeycomb with all 7 spans correctly nested
- [ ] Three observations in baseline report grounded in actual span data (not speculation)
- [ ] Runbook tested by a different person re-running the steps from scratch

---

## Coordination notes

- **PR-per-repo.** Don't bundle. Four separate PRs:
  1. orchet-backend (spans for STT, orchestrator turn, TTS)
  2. orchet-web (client-side spans)
  3. orchet-ios (client-side spans)
  4. orchet-voice (baseline doc + runbook + optional Sarvam harness)

- **Reviewer:** Kalas (Prasanth Kalas) on all four PRs. Claude can pre-review against the architecture doc on request.

- **Communication:** post status updates in the same thread that dispatched this brief. Surface blockers immediately — don't try to work around them silently. Specific things that warrant a status message:
  - Existing tests fail that you didn't touch (might be flakes or recent regressions — flag, don't fix unrelated)
  - Sampling settings or Honeycomb account access issues
  - Sarvam blockers (skip the harness; note in the report)
  - Any deviation from the 7 span names (don't deviate; raise the question)

- **Out-of-scope discoveries.** If during instrumentation you find an obvious bug or perf win in the current voice path, DO NOT fix it in this PR. Open a separate issue/task in the relevant repo and link from your final report. We want Phase 0 to be a clean measurement increment; mixing in perf fixes pollutes the baseline.

- **Stop condition — route paths.** If the current voice flow in `orchet-web` or `orchet-ios` calls routes that materially differ from what this brief assumes (e.g., the brief mentions `/integrations/stt` but reality is `/voice/upload-audio`), **stop and report**. Use the actual route that exists in the code. **Do not invent or rename `/api/*` paths.** The goal is to measure reality, not to reshape it.

---

## Definition of done

Phase 0 is complete when:

1. All 7 spans are live in production (or staging if production traffic is too thin) and visible in Honeycomb
2. Dashboard exists with 5 panels per spec
3. `Orchet-AI/orchet-voice/docs/phase-0-baseline.md` is committed with either real numbers or a clear placeholder + runbook
4. `Orchet-AI/orchet-voice/docs/phase-0-runbook.md` is committed and tested by a different person
5. (Optional) Sarvam smoke harness committed if no blockers
6. All four PRs merged to main on their respective repos
7. Status report posted summarizing: what was instrumented, the headline p50 number, the dominant stage, and the worst geographic region

After Phase 0 is reported complete, Phase 1 (`orchet-voice` skeleton on Fly.io) can begin. The Phase 0 numbers inform Phase 2 success criteria but do not block Phase 1 from starting in parallel.

---

## References

- [VOICE-ARCHITECTURE-1 ADR v6](../architecture/VOICE-ARCHITECTURE-1.md) — strategic context, hard rules, fallback decisions
- [Phase 0 measurement plan](../phase-0-measurement-plan.md) — original plan this brief implements
- [Sarvam evaluation plan](../sarvam-evaluation-plan.md) — full Sarvam smoke spec (Phase 0 implements the minimum-viable subset)
- [Voice-turn contract proposal](../voice-turn-contract-proposal.md) — Phase 3 only; mentioned here for context
- [Repo scaffold plan](../repo-scaffold-plan.md) — Phase 1 only; mentioned here for context
