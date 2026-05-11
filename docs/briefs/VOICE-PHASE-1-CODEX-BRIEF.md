# Codex brief — VOICE-PHASE-1: Skeleton + Fly.io deploy + India RTT probe

**Brief ID:** VOICE-PHASE-1-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Scaffold plan:** [repo-scaffold-plan.md](../repo-scaffold-plan.md)
**Status:** Dispatched (2026-05-11)
**Owner:** Codex
**Reviewer:** Kalas (CEO/CTO) + Claude
**Estimated effort:** 5 days

This brief is self-contained. Read the parent ADR (link above) for strategic context but you can execute everything below without re-deriving any decisions.

---

## Goal

Stand up the `orchet-voice` service skeleton on Fly.io. Prove the long-lived WebRTC transport works end-to-end with **echo only** (no STT/LLM/TTS yet). Measure WebRTC RTT from a real Indian network endpoint via a probe Machine in Mumbai. Decide Phase 2 / Phase 4 scheduling based on India RTT.

You are NOT building the real voice pipeline yet. STT, LLM, TTS, function calling, Sarvam — all Phase 2+.

---

## Hard scope boundaries

**You MUST NOT:**
- Wire any STT / TTS / LLM provider (Phase 2)
- Add Pipecat function-calling / tool invocation (Phase 3)
- Add Sarvam (Phase 4 — already in repo planning docs)
- Implement `/voice/turn` calls to backend (Phase 3)
- Add iOS WebRTC client (Phase 6)
- Make changes to `orchet-backend`, `orchet-web`, or `orchet-ios` (they're complete for Phase 0)
- Deploy to any region besides `iad` (primary) and `bom` (probe-only — see below)
- Add `swift-otel` or change anything in `orchet-ios`

**You MUST:**
- Use Python 3.12 + FastAPI + Pipecat OSS pinned to **0.0.61** (do not upgrade past — known regression)
- Use Daily WebRTC transport adapter
- Echo pipeline only (audio frames in → audio frames out, NO STT/LLM/TTS in the pipeline)
- Validate Supabase Bearer JWT on signaling-connect
- Emit OpenTelemetry traces to Honeycomb (the `LUMO_OTEL_*` Fly secrets are already set)
- Deploy via Fly.io (app `orchet-voice` already exists; secrets already set — see below)
- Single region `iad` for the production echo + a separate Machine in `bom` for the India RTT probe
- All four verification steps green (`uv sync` / `ruff check` / `pyright` / `pytest`)

---

## Pre-provisioned infrastructure (do not re-create)

The following are **already in place** — do not recreate or you'll fragment state:

| Resource | Identifier | Notes |
|---|---|---|
| Fly app | `orchet-voice` (org `orchet`) | Status pending; no machines yet. Your `fly deploy` lands the first Machine. |
| Fly org token | `FLY_API_TOKEN` env | Long-lived (expires 2027-05-11). Use for CI deploy. Don't generate a new one. |
| Daily Cloud account | subdomain `orchet.daily.co` | Free-tier $15 credit, more than enough for Phase 1 testing. |
| Honeycomb dashboard | board `kvRUiNXYcvk` | Voice spans you emit will populate it once they flow through `lumo-ml-service` dataset. |

**14 Fly secrets already set on `orchet-voice` (do not re-set):**

```
ANTHROPIC_API_KEY              GROQ_API_KEY
DAILY_API_KEY                  LUMO_DEEPGRAM_API_KEY
DAILY_ROOM_DOMAIN              LUMO_OTEL_ENDPOINT
NEXT_PUBLIC_SUPABASE_ANON_KEY  LUMO_OTEL_HEADERS
NEXT_PUBLIC_SUPABASE_URL       ORCHET_GATEWAY_URL
ORCHET_HONEYCOMB_API_KEY       ORCHET_INTERNAL_TOKEN
ORCHET_VOICE_ENV               ORCHET_VOICE_LLM_DEFAULT
```

Don't set `DAILY_*` or any of the above unless you discover one is missing — confirm via `fly secrets list -a orchet-voice` if uncertain. If you do find one missing, set it via `fly secrets set` rather than committing it to the repo.

---

## Phase 1 deliverables (per repo-scaffold-plan.md)

### 1a — Repo scaffold (~ 1 day)

Implement the directory layout from [repo-scaffold-plan.md § Directory layout](../repo-scaffold-plan.md#directory-layout):

```
voice/
├── __init__.py
├── server.py
├── pipeline.py             # echo-only for now
├── transport.py            # Daily transport wiring
├── auth.py                 # Bearer JWT validation
├── routes/
│   ├── health.py
│   └── debug.py            # dev-only echo trigger
└── obs/
    ├── tracing.py
    └── logging.py
tests/
├── conftest.py
├── test_health.py
├── test_auth.py
└── test_pipeline_echo.py
Dockerfile
fly.toml
pyproject.toml
uv.lock
.env.example
.github/workflows/ci.yml
.github/workflows/deploy.yml
```

Dependencies pinned exactly per [repo-scaffold-plan.md § Dependencies](../repo-scaffold-plan.md#dependencies-initial). Most importantly: `pipecat-ai==0.0.61` (do not upgrade).

### 1b — US East deploy + echo round-trip (~ 2 days)

- WebRTC signaling endpoint reachable at `wss://orchet-voice.fly.dev` (custom domain `voice.orchet.ai` is Phase 5 — don't set up DNS in this PR)
- Bearer JWT validated on connection-open via Supabase
- Echo pipeline: audio frames in → same frames out, **no STT/LLM/TTS**
- Health endpoint `/health` returns the shape from [repo-scaffold-plan.md § Health endpoint](../repo-scaffold-plan.md#health-endpoint)
- OpenTelemetry traces flowing to Honeycomb with span name `voice.echo.roundtrip` (just one span — pipeline level — for Phase 1)
- `min_machines_running=1` (always-on; per ADR section "Phase 1 single region")
- A simple smoke-test page in `tests/smoke/web-client.html` (vanilla JS + Daily JS SDK) that:
  - Connects to the WebRTC endpoint with a Supabase JWT
  - Captures mic for 5 seconds
  - Plays back the echoed audio
  - Logs round-trip latency

**Output:** `wss://orchet-voice.fly.dev` reachable, JWT working, RTT measured end-to-end from a US East client (you, presumably).

### 1c — India RTT probe (~ 2 days)

- Deploy a **second** Fly Machine to `bom` (Mumbai) using the same Docker image
- Use the smoke-test page from 1b, point it at the bom Machine via Fly's `[[services]]` region-pinning or a separate hostname like `orchet-voice-bom.fly.dev`
- Coordinate one of these test scenarios:
  - Test from a real Indian network (VPN to India, or Kalas's home network — coordinate)
  - OR: run a probe Machine that pings the bom region externally and reports approximate RTT
- Record p50 RTT in `docs/phase-1-india-probe-result.md` with this exact structure:

```markdown
# Phase 1b — India RTT probe result

**Measured:** YYYY-MM-DD
**Probe method:** <real client / synthetic ping / etc>
**Endpoint:** <which Fly Machine / hostname was tested>
**Sample count:** N

## Result

| Stage | p50 | p95 |
|---|---|---|
| WebRTC handshake RTT (India → bom Machine) | — | — |
| Echo round-trip (mouth-to-ear, India → bom → India) | — | — |

## Decision per VOICE-ARCHITECTURE-1 v6

- [ ] p50 RTT < 200 ms → APAC stays in Phase 5 (multi-region rollout)
- [ ] p50 RTT 200–400 ms → APAC promotes ahead of Sarvam scheduling
- [ ] p50 RTT > 400 ms → APAC promotes to Phase 2 blocker
- [ ] **Selected:** _____________
```

After the probe is done, leave the `bom` Machine running so it can be reused in Phase 4/5, but reduce it to `min_machines_running=0` if cost is a concern (note: Phase 4/5 needs it always-on; document the choice).

---

## Stop conditions (must report, not work around)

- **Pipecat 0.0.61 doesn't install with Python 3.12** — try Python 3.11 instead and report; ADR allows that fallback
- **Daily WebRTC transport adapter version mismatch with Pipecat 0.0.61** — pin a compatible Daily transport version and report
- **Fly app `orchet-voice` is missing or in a state you can't recover from** — DO NOT delete and recreate; surface this and wait for human help (might be permission scope on the token)
- **Honeycomb dataset routing is wrong** — voice spans need to land in `lumo-ml-service` dataset. The OTel env vars already point there. If you see spans landing elsewhere, document and report
- **India probe gives p50 < 50ms or > 1000ms** — both are suspicious; rerun the probe; document the methodology before drawing conclusions
- **Any required secret is missing** — `fly secrets list -a orchet-voice` should show all 14 from above. If one is missing, set it via `fly secrets set` and note in the PR description; do NOT commit the value to the repo

---

## Verification checklist

Before opening the PR:

- [ ] `uv sync --frozen` passes (deps locked + resolves clean)
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass
- [ ] `uv run pyright voice/ tests/` passes
- [ ] `uv run pytest -v` passes (4+ tests: health, auth, echo pipeline, smoke)
- [ ] Docker image builds (`docker build .` succeeds)
- [ ] `fly deploy --strategy rolling` to `iad` succeeds
- [ ] `curl https://orchet-voice.fly.dev/health` returns the documented JSON shape
- [ ] WebRTC echo smoke test passes from a Daily JS SDK web client with a real Supabase JWT
- [ ] Honeycomb shows `voice.echo.roundtrip` span on the dashboard within 1 minute of running the smoke test
- [ ] No secrets in diff (audit `git diff main`)
- [ ] No PII or audio samples committed
- [ ] `bom` probe Machine deployed + RTT measured

---

## PR structure

Single PR to `Orchet-AI/orchet-voice`:

**Title:** `VOICE-PHASE-1: skeleton + Fly.io echo + India RTT probe`

**Body:**
- What you implemented (5–10 bullets)
- Smoke test result (RTT from US East)
- India RTT probe result (link to `docs/phase-1-india-probe-result.md`)
- Decision per Phase 1b decision tree
- Any deviations from the brief or scaffold plan, with reasoning
- Open questions / followups for Phase 2

Tag `@Prasanth-Kalas` as reviewer. Update the tracking issue (to be created at issue #2 in `orchet-voice`) with status as you progress.

---

## Reviewer expectations

When the PR opens, Claude will spot-check:

- File layout matches [repo-scaffold-plan.md § Directory layout](../repo-scaffold-plan.md#directory-layout)
- `pipecat-ai==0.0.61` pinned in `pyproject.toml` and `uv.lock`
- Echo pipeline does NOT reference Deepgram / Groq / Aura-2 / Sarvam / function calling
- Daily transport adapter wired correctly
- Bearer JWT validation actually verifies against Supabase (not a mock)
- `fly.toml` has `min_machines_running=1`, `auto_stop_machines=off`
- `voice.echo.roundtrip` span emitted correctly
- India probe result is filled in honestly (real measurement, not placeholder)
- Decision tree result is sensible

Failing any of these = revisions before merge.

---

## What "done" looks like

Phase 1 is complete when:

1. PR merged to `orchet-voice` main
2. `wss://orchet-voice.fly.dev` reachable, accepts a Daily WebRTC connection with a valid Supabase JWT, and echoes audio back
3. `docs/phase-1-india-probe-result.md` committed with real RTT numbers + Phase 2/4/5 scheduling decision
4. Honeycomb dashboard shows at least one `voice.echo.roundtrip` span
5. Status report posted on tracking issue summarizing RTT result + decision

After Phase 1 closes, Phase 2 (Deepgram STT + Groq LLM + Aura-2 TTS + barge-in) becomes the next dispatchable lane. Phase 1's India probe result feeds directly into Phase 2 scheduling.

---

## References

- [VOICE-ARCHITECTURE-1 ADR v6](../architecture/VOICE-ARCHITECTURE-1.md)
- [repo-scaffold-plan.md](../repo-scaffold-plan.md) — full directory layout, deps, fly.toml example
- [phase-0-baseline.md](../phase-0-baseline.md) — REST voice path baseline (compare your echo RTT against this)
- [phase-0-runbook.md](../phase-0-runbook.md) — telemetry verification pattern
- [Pipecat 0.0.61](https://github.com/pipecat-ai/pipecat/releases/tag/v0.0.61) — pinned version
- [Daily JS SDK docs](https://docs.daily.co/reference/daily-js)
- [Fly.io regions](https://fly.io/docs/reference/regions/) — `iad` + `bom` are the two used here
