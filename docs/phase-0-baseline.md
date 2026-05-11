# Phase 0 — Voice baseline

**Measured:** 2026-05-11
**Dashboard:** TODO — create dashboard from [phase-0-honeycomb-dashboard-spec.yaml](./phase-0-honeycomb-dashboard-spec.yaml)
**Traffic window:** TODO — last 24h after backend, web, and iOS instrumentation are deployed together
**Sample count:** 0 verified web + 0 verified ios turns in this Codex run; needs 50 web + 50 ios measured turns to fill

## Headline numbers

| Stage | p50 | p95 | p99 |
|---|---:|---:|---:|
| voice.total.mouth_to_ear | TODO — needs 50 web + 50 ios turns | TODO | TODO |
| voice.client.capture | TODO — needs 50 web + 50 ios turns | TODO | TODO |
| voice.upload | TODO — needs 50 web + 50 ios turns | TODO | TODO |
| voice.stt.batch | TODO — needs 50 web + 50 ios turns | TODO | TODO |
| voice.orchestrator.turn | TODO — needs 50 web + 50 ios turns | TODO | TODO |
| voice.tts.batch | TODO — needs 50 web + 50 ios turns | TODO | TODO |
| voice.client.play | TODO — needs 50 web + 50 ios turns | TODO | TODO |

## By geography

| Region | total p50 | total p95 | sample count |
|---|---:|---:|---:|
| US | TODO — needs measured traffic | TODO | TODO |
| EU | TODO — needs measured traffic | TODO | TODO |
| India / SEA | TODO — needs measured traffic | TODO | TODO |

## Three observations

1. **Dominant stage by p50 latency** — Pending Honeycomb data. Declare this only after the per-stage p50 panel has at least 50 web turns and 50 ios turns in the same 24h window.
2. **Worst stage by p99 / p50 ratio** — Pending Honeycomb data. Use the tail-latency panel to compute p99 / p50 per span and flag the largest ratio, not the largest absolute p99.
3. **Worst region** — Pending Honeycomb data. Compare `voice.total.mouth_to_ear` p50 and p95 by `client.ip.region`; call a region materially worse only if the gap is sustained across enough turns to rule out a single slow local network.

## What this tells us about the new architecture targets

- Phase 0 is now ready to produce the baseline, but this Codex environment did not have Honeycomb web UI or API access, so no live percentile table could be pulled into this PR.
- The measurement target is still the current REST path: client capture, upload/STT, orchestrator turn, batch TTS, and client playback. The Phase 1+ WebRTC/Pipecat work should be compared against the filled p50/p95/p99 rows above.
- The most important comparison is `voice.total.mouth_to_ear` by `client.kind` and geography. If India / SEA totals are already near or above the ADR's 1.2s Phase 1 p50 assumption, the Phase 1b Mumbai RTT probe should be treated as a hard gate.
- Backend spans are queryable through the standard Honeycomb span stream. Client spans may arrive through browser performance telemetry or iOS unified-log/signpost ingestion depending on the deployment path; keep the exact span names unchanged when materializing the dashboard.

## Caveats

- Dashboard creation is blocked on Honeycomb web UI access. Build it from [phase-0-honeycomb-dashboard-spec.yaml](./phase-0-honeycomb-dashboard-spec.yaml), then replace the dashboard TODO above with the real URL.
- No Honeycomb data was queried from this Codex run because no Honeycomb UI or API credential was available in the environment.
- Do not interpret placeholder rows as latency results. Re-run the runbook after deployed traffic or synthetic turns accumulate.
- Client-to-backend distributed trace nesting is pragmatic, not perfect. Phase 0 correlation relies on `voice.session_id`, `voice.turn_id`, and `client.kind` wherever traceparent propagation was not already present.
- iOS Phase 0 spans use `OSSignposter` plus structured logs rather than a full swift-otel exporter, by brief scope.
- Sarvam smoke was skipped for this PR: no Sarvam account/API access or approved local sample-audio source was available, and the brief says to skip rather than commit keys or real audio.
