# Phase 0 — Voice measurement runbook

This runbook refreshes the Phase 0 baseline for the existing REST voice path. It does not exercise the future WebRTC / Pipecat service.

## Prerequisites

- Backend, web, and iOS Phase 0 instrumentation PRs are merged and deployed to the environment being measured.
- Honeycomb access for the Orchet dataset that receives backend traces and client voice telemetry.
- A signed-in Orchet test user with permission to use voice mode.
- For curl-only backend smoke checks, a local Supabase access token and gateway base URL:

```sh
export ORCHET_GATEWAY_URL="https://api.orchet.ai"
export SUPABASE_ACCESS_JWT="<test-user-jwt>"
```

## Trigger a Test Voice Turn

### Full web turn

1. Open the deployed web app or a local `orchet-web` checkout pointed at the target gateway.
2. Sign in as the test user.
3. Open the chat surface and enable voice mode.
4. Tap the mic and say a short prompt, for example: "Plan a two day Mumbai trip next weekend."
5. Wait until the assistant starts speaking.
6. Repeat 50 times for web if you need a fresh baseline window.

Expected spans for a successful web turn:

- `voice.client.capture`
- `voice.upload`
- `voice.stt.batch`
- `voice.orchestrator.turn`
- `voice.tts.batch`
- `voice.client.play`
- `voice.total.mouth_to_ear`

### Full iOS turn

1. Install a build that includes the Phase 0 iOS instrumentation.
2. Sign in as the same kind of test user.
3. Open the chat surface.
4. Press or tap voice input, speak a short prompt, then release or let silence endpointing finish.
5. Wait until the assistant starts speaking.
6. Repeat 50 times for ios if you need a fresh baseline window.

iOS emits the same four client span names through `OSSignposter` intervals and structured logs. Verify the telemetry drain maps those exact names into Honeycomb before using iOS numbers in the baseline.

### Curl backend smoke

Curl checks are useful for confirming backend spans, but they do not produce client capture/play spans.

Generate a local synthetic WAV on macOS:

```sh
say -v Samantha "Plan a two day Mumbai trip next weekend." -o /tmp/orchet-voice-smoke.aiff
afconvert -f WAVE -d LEI16@16000 /tmp/orchet-voice-smoke.aiff /tmp/orchet-voice-smoke.wav
```

Run STT:

```sh
curl -sS \
  -H "Authorization: Bearer $SUPABASE_ACCESS_JWT" \
  -F "audio=@/tmp/orchet-voice-smoke.wav;type=audio/wav" \
  "$ORCHET_GATEWAY_URL/stt"
```

Run TTS:

```sh
curl -sS \
  -H "Authorization: Bearer $SUPABASE_ACCESS_JWT" \
  -H "Content-Type: application/json" \
  -d '{"text":"I can help plan that Mumbai trip.","voice_id":"aura-2-thalia-en"}' \
  "$ORCHET_GATEWAY_URL/tts" \
  -o /tmp/orchet-voice-tts-output.audio
```

Run orchestrator chat:

```sh
curl -N -sS \
  -H "Authorization: Bearer $SUPABASE_ACCESS_JWT" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"session_id":"phase-0-smoke","messages":[{"role":"user","content":"Plan a two day Mumbai trip next weekend."}],"device_kind":"web","region":null}' \
  "$ORCHET_GATEWAY_URL/chat"
```

## Verify Spans in Honeycomb

Use a 24h window for the first check. Start broad, then narrow.

1. Filter backend voice spans:
   - `service.name = orchet-backend`
   - `span.name starts-with voice.`
2. Confirm the backend spans exist:
   - `voice.stt.batch`
   - `voice.orchestrator.turn`
   - `voice.tts.batch`
3. Confirm client spans exist in the same dataset or client telemetry dataset:
   - `voice.client.capture`
   - `voice.upload`
   - `voice.client.play`
   - `voice.total.mouth_to_ear`
4. Group by `voice.turn_id` and inspect at least one turn. The same turn should have the client spans plus matching backend spans where correlation headers or trace context are present.
5. Group by `client.kind` and confirm both `web` and `ios` appear after test turns have run.

If Honeycomb uses `name` instead of `span.name` in the selected dataset, apply the same filters with that field name. Keep dashboard labels as the canonical span names.

## Refresh the Baseline Report

1. Open the dashboard created from [phase-0-honeycomb-dashboard-spec.yaml](./phase-0-honeycomb-dashboard-spec.yaml).
2. Set the time window to the measured 24h interval.
3. Confirm sample counts:
   - at least 50 web turns
   - at least 50 ios turns
   - enough geography coverage to make a regional claim
4. Copy p50, p95, and p99 for all seven spans into [phase-0-baseline.md](./phase-0-baseline.md).
5. Copy total p50/p95 and sample counts into the geography table.
6. Fill the three observations:
   - stage with highest p50
   - stage with highest p99 / p50 ratio
   - region with materially worse total latency
7. Replace the dashboard TODO with the real Honeycomb dashboard URL.
8. Commit the refreshed report in a docs-only PR.

## Known Limitations

- Client spans are correlated pragmatically with `voice.session_id`, `voice.turn_id`, and `client.kind` where traceparent was not already wired.
- iOS Phase 0 uses `OSSignposter` and structured logs, not a new swift-otel SDK.
- Curl checks do not produce end-to-end mouth-to-ear numbers.
- Synthetic turns should not be mixed with production-user turns unless the report labels the traffic source.
- If sampling is reduced in production, p99 may be unstable. Use staging with 100% sampling for baseline collection if production traffic is thin or sampled.
- Do not commit audio samples, real transcripts, API keys, Supabase JWTs, or provider credentials to this repository.
