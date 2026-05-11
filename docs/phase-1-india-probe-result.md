# Phase 1b — India RTT probe result

**Measured:** 2026-05-11
**Probe method:** Synthetic HTTPS health-check RTT from the Codex machine on an India network path. Requests used `Fly-Prefer-Region: bom`; `/health` returned `region: bom` and the Fly request id ended in `-bom`.
**Endpoint:** `https://orchet-voice.fly.dev/health` routed to Fly Machine `80e473b6591498` (`phase1-bom-probe`, region `bom`).
**Sample count:** 30

## Result

| Stage | p50 | p95 |
|---|---|---|
| WebRTC handshake RTT (India → bom Machine) | 135 ms synthetic HTTPS RTT proxy | 707 ms synthetic HTTPS RTT proxy |
| Echo round-trip (mouth-to-ear, India → bom → India) | Not completed: requires real Supabase user JWT + browser mic smoke | Not completed |

## Decision per VOICE-ARCHITECTURE-1 v6

- [x] p50 RTT < 200 ms → APAC stays in Phase 5 (multi-region rollout)
- [ ] p50 RTT 200–400 ms → APAC promotes ahead of Sarvam scheduling
- [ ] p50 RTT > 400 ms → APAC promotes to Phase 2 blocker
- [x] **Selected:** Provisional APAC stays in Phase 5 based on synthetic `bom` p50 = 135 ms. Final confirmation still needs the browser WebRTC echo smoke with a real Supabase user JWT.

## Methodology notes

- Fly deployment used `fly deploy --remote-only --strategy rolling`; no local Docker daemon was used.
- Fly remote builder produced image `registry.fly.io/orchet-voice:deployment-01KRBV35DV4VYVMHBCZ265QJ89`.
- The first deploy automatically created two healthy `iad` Machines because Fly deploy defaults to `--ha=true`. Extra Machine `e784500dc29983` was removed after the `bom` clone to keep Phase 1 at one always-on `iad` Machine plus one `bom` probe Machine. The deploy workflow now uses `--ha=false`.
- The `bom` probe Machine was cloned from `e2862e7eb72186` and left running for Phase 4/5 reuse. The service reports region from `ORCHET_VOICE_REGION` when explicitly set, otherwise from Fly's per-Machine `FLY_REGION`.
- India synthetic probe used 30 sequential `curl` samples with `time_total` against `/health`, 150 ms spacing between samples.
- `bom` sample range: min 108 ms, p50 135 ms, p95 707 ms, max 1231 ms. Stop-condition rerun was not required because p50 was between 50 ms and 1000 ms.
- Comparison only: India endpoint to `iad` with `Fly-Prefer-Region: iad` measured p50 387 ms and p95 694 ms over the same synthetic method. This is not a US East client measurement.
- Real Daily WebRTC echo smoke was not completed in this environment because `/debug/echo` correctly requires a Supabase Bearer user JWT and the smoke page requires browser microphone/audio playback.
