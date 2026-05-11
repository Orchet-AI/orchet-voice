# Codex brief — VOICE-PHASE-6: iOS Daily WebRTC cutover + native confirmation modal

**Brief ID:** VOICE-PHASE-6-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Predecessors:** Phase 3 merged + `NEXT_PUBLIC_VOICE_MODE_BACKEND=streaming` flipped on web + 24h Honeycomb soak passed cleanly. Phase 4 (Sarvam) and Phase 5 (multi-region) may be in flight or merged — they do not block this brief.
**Status:** Drafted 2026-05-11
**Owner:** Codex
**Reviewer:** Kalas + Claude
**Estimated effort:** 7-10 days

This is the final user-cutover phase. Web is already on the streaming pipeline (Phase 3); iOS still goes through `apps/web /api/stt` + `/api/tts` (Phase 0 batch path). After this PR merges and the iOS feature flag flips, both web and iOS users hit `orchet-voice.fly.dev` directly. After that, the gateway `/stt` and `/tts` legacy routes can be retired in a separate cleanup PR.

---

## Predecessor gates

Do NOT start this brief until:

1. **Phase 3 PR 3 merged** on `Orchet-AI/orchet-web@main` (commit `d0af266` or later).
2. **`NEXT_PUBLIC_VOICE_MODE_BACKEND=streaming`** flipped on the production Vercel project. Web users have been on the new pipeline for at least 24h.
3. **Honeycomb soak board** ([saZfB6QUaYM](https://ui.honeycomb.io/lumo/environments/test/board/saZfB6QUaYM)) shows mouth-to-ear p50 < 1s and barge-in p95 < 300ms holding cleanly over those 24h.
4. **No P1 voice incidents** in those 24h.

If any of the four are false: STOP. The whole point of staging web first is to derisk iOS.

---

## Goal

Add a Daily WebRTC streaming voice path to `orchet-ios`, gated behind a runtime feature flag. The legacy capture-and-upload path stays alive for fallback and for users on builds without the flag set. Keep the deploy boring: ship the code, leave the flag default-off, and flip via the existing `Lumo.local.xcconfig` mechanism per build.

---

## Hard scope boundaries

**You MUST NOT:**
- Delete `DeepgramTokenService`, `TextToSpeechService`, `VoiceComposerViewModel`, or `SpeechModeGating` — they remain the batch-path implementation behind the flag.
- Remove the gateway `/stt` or `/tts` routes (separate cleanup PR after Phase 6 stabilizes).
- Change the voice service contract — orchet-voice's `/debug/echo`, `/voice/turn`, `/voice/confirm-action` are stable.
- Add Sarvam-specific iOS code — Phase 4 wires Sarvam server-side; iOS just sends audio + locale and lets the voice service route.
- Bump `IPHONEOS_DEPLOYMENT_TARGET` above 17.0 (the Daily Swift SDK supports iOS 13+; we're well past that).
- Add CocoaPods (this project is SPM-only — keep it that way).

**You MUST:**
- Add a `LumoVoiceBackend` enum (`.streaming`, `.batch`) with default `.batch`, read at app launch from `Info.plist` key `OrchetVoiceMode` (string).
- Populate the existing `Info.plist` key `OrchetVoiceBase` so the streaming path knows where to connect.
- Add the **Daily Swift SDK** as an SPM dependency in `project.yml`.
- Build a new `StreamingVoiceService` that mirrors the web `StreamingVoiceMode` shape — Daily call object, audio in/out, native barge-in.
- Build a native `VoiceConfirmationView` that mounts when a `show_confirmation` Daily app message arrives. Mirror the web modal's UX: title, summary, label/value details, Confirm + Cancel, auto-expire.
- After the user taps Confirm/Cancel, POST to `${LumoAPIBase}/voice/confirm-action` with the Supabase JWT, then `sendAppMessage` a `confirmation_resolved` event back over Daily.
- Implement native VAD via `AVAudioEngine`'s `installTap` — RMS-based threshold detector. **Do NOT use Silero on iOS** — too much WASM/ONNX baggage for what we need. Send `barge_in` app messages on speech start/end same as the web client.
- Background audio session: configure `.playAndRecord` category, with `.mixWithOthers` if user has music playing (Spotify integration coexists).
- Match the existing iOS architecture pattern: `Lumo/Services/StreamingVoiceService.swift`, `Lumo/ViewModels/StreamingVoiceViewModel.swift`, `Lumo/Components/VoiceConfirmationView.swift`, `Lumo/Views/StreamingVoiceView.swift`.
- Branch on the flag at `Lumo/Components/ChatComposerTrailingButton.swift` (or wherever today's voice button entry-point lives) — render the streaming button when `.streaming`, the existing push-to-talk when `.batch`.

---

## Deliverable: single PR to `orchet-ios`

**Title:** `VOICE-PHASE-6: iOS Daily WebRTC cutover + native confirmation modal + feature flag`

### Part A — SPM dep + Info.plist + xcconfig

1. Add to `project.yml` under `packages:`

       Daily:
         url: https://github.com/daily-co/daily-client-ios
         from: "0.27.0"

   (Verify the exact tag/branch by inspecting the daily-client-ios releases page — pick the latest 0.x stable. If a 1.0 has shipped, prefer that.)

2. Regenerate `Lumo.xcodeproj` with XcodeGen and commit the resulting changes.

3. `Info.plist` additions:

       <key>OrchetVoiceMode</key>
       <string>$(ORCHET_VOICE_MODE)</string>
       <key>OrchetVoiceBase</key>
       <string>$(ORCHET_VOICE_BASE)</string>

   (Both already template-shaped via `xcconfig`. Make sure `Lumo.xcconfig` declares `ORCHET_VOICE_MODE` defaulting to `batch` and `ORCHET_VOICE_BASE` defaulting to `https://orchet-voice.fly.dev`. Add these to `scripts/ios-write-xcconfig.sh` so CI / fresh clones get sensible defaults.)

4. Background-audio mode: `Info.plist` `UIBackgroundModes` already includes `remote-notification`, `fetch`, `processing`. Add `audio` so the Daily call survives screen lock during a voice turn.

### Part B — feature flag + service entry point

5. New file `Lumo/Services/VoiceBackendConfig.swift`:

       enum LumoVoiceBackend: String { case streaming, batch }

       struct VoiceBackendConfig {
         static let current: LumoVoiceBackend = {
           let raw = Bundle.main.object(forInfoDictionaryKey: "OrchetVoiceMode") as? String
           return LumoVoiceBackend(rawValue: raw ?? "") ?? .batch
         }()

         static var voiceServiceBaseURL: URL {
           let raw = Bundle.main.object(forInfoDictionaryKey: "OrchetVoiceBase") as? String ?? "https://orchet-voice.fly.dev"
           return URL(string: raw)!
         }
       }

   This is the single source of truth. Every voice-related view consults `VoiceBackendConfig.current` once at mount, identical pattern to web's `useVoiceBackend()`.

### Part C — StreamingVoiceService

6. New file `Lumo/Services/StreamingVoiceService.swift`. Responsibilities:

   - Open a `URLSession` POST to `${voiceServiceBaseURL}/debug/echo` with `Authorization: Bearer <Supabase access token>` and body `{ client_kind: "ios", ttl_seconds: 600 }`.
   - Parse response: `{ voice_session_id, room_url, client_token, expires_at, region }`.
   - Use `Daily.CallClient` (the iOS SDK's main entry point) to `.join(url: room_url, token: client_token, settings: <mic-on, camera-off>)`.
   - Wire `AVAudioSession`: `.playAndRecord` category, `.measurement` mode, `.mixWithOthers` option set when Spotify is the active audio source. Activate before joining the call.
   - Native VAD: install a tap on the input node, compute RMS over a 10-30 ms window, debounce. On speech-start → `callClient.sendAppMessage(["type": "barge_in", "state": "speech_started", "voice_session_id": ..., "turn_id": <uuid-v4>, "client_kind": "ios", "client_sent_at": <ms>])`. Same on speech-end.
   - Subscribe to the Daily call's `appMessage` callback. When a payload's `type == "show_confirmation"` arrives, hand off to the confirmation view model. When `type == "confirmation_resolved"` — voice service emits this so iOS doesn't need to (it's the web → voice direction). Ignore on iOS.
   - On leave: tear down the call, deactivate the audio session, restore prior session category.

7. New file `Lumo/ViewModels/StreamingVoiceViewModel.swift` — `@MainActor` `ObservableObject` that wraps `StreamingVoiceService`. State machine identical to web's: `off`, `connecting`, `listening`, `agent_speaking`, `error`.

### Part D — confirmation modal

8. New file `Lumo/Components/VoiceConfirmationView.swift`. Mirror the web modal:

   - Title, summary, label/value detail list from the Daily app message's `confirmation_payload`.
   - Confirm button → POST `${LumoAPIBase}/voice/confirm-action` with Supabase JWT and body `{ session_id, confirmation_id, accepted: true }`.
   - Cancel button → same POST with `accepted: false`.
   - On success, `sendAppMessage` a `confirmation_resolved` Daily message:
     `{ "type": "confirmation_resolved", "confirmation_id": ..., "result": "executed"|"cancelled", "voice_continuation_hint": <response.voice_continuation_hint> }`.
   - Auto-cancel timer based on `expires_at`.

9. New file `Lumo/Views/StreamingVoiceView.swift` — the SwiftUI container that hosts the call state UI + presents the confirmation modal as a `.sheet` or `.fullScreenCover` (your call; whichever matches the existing voice UX in `VoiceComposerView`).

### Part E — flag-based dispatch in the existing UI

10. Edit `Lumo/Components/ChatComposerTrailingButton.swift` (or the file that currently renders the voice button) to branch on `VoiceBackendConfig.current`:

        switch VoiceBackendConfig.current {
        case .streaming:
            return AnyView(StreamingVoiceButton(viewModel: streamingVM))
        case .batch:
            return AnyView(VoicePushToTalkButton(viewModel: existingVM))
        }

11. Do NOT change `VoiceComposerViewModel`, `DeepgramTokenService`, `TextToSpeechService`, or `SpeechModeGating`. The batch branch must continue to function unchanged.

### Part F — tests

12. Add XCTest cases under `LumoTests/`:

    - `VoiceBackendConfigTests.swift` — fixture Info.plist values parse correctly into `.streaming` / `.batch`; invalid / missing → `.batch`.
    - `StreamingVoiceSessionResponseTests.swift` — decode the `/debug/echo` JSON shape.
    - `VoiceConfirmationViewModelTests.swift` — confirmation_payload parse, confirm/cancel POSTs construct the right body, auto-expire timer fires.
    - `BargeInRMSDetectorTests.swift` — feed synthetic audio buffers (silence, then loud), assert state transitions.

    Mock the network with `URLProtocol` or a small test-stub. Mock Daily.CallClient with a protocol-based seam (Daily SDK's main class isn't trivially mockable; introduce a `CallClientProtocol` that wraps the real SDK).

13. UI smoke (optional but encouraged): one XCUITest that launches the app with `ORCHET_VOICE_MODE=streaming` set via launch arguments, taps the voice button, and asserts the connecting state appears. Without an actual mic in CI, just verify the UI state machine reaches `connecting`.

---

## Verification

```
# Same CI shape that's already in .github/workflows/ci.yml
xcodegen generate
xcodebuild build -scheme Lumo \
                 -destination 'platform=iOS Simulator,name=iPhone 15,OS=latest' \
                 CODE_SIGNING_ALLOWED=NO
xcodebuild test  -scheme Lumo \
                 -destination 'platform=iOS Simulator,name=iPhone 15,OS=latest' \
                 CODE_SIGNING_ALLOWED=NO
```

All three must pass.

Manual smoke (do BEFORE marking the PR ready-for-review; capture screenshots in the PR body):

1. Set `ORCHET_VOICE_MODE=streaming` in `Lumo.local.xcconfig`. Build to a simulator + a real device.
2. Sign in. Open voice mode. Verify the streaming UI appears (not the legacy push-to-talk).
3. Say "what time is it in Tokyo?" — expect a spoken response within ~1.5s mouth-to-ear.
4. Say "book me a flight to Tokyo tomorrow" — expect the spoken hint, then the native confirmation modal mounts with the booking details. Tap Confirm. Expect the voice to continue with "Done. Confirmation code …".
5. Talk OVER the TTS. Expect the assistant to stop within ~300 ms (barge-in works natively).
6. Switch `ORCHET_VOICE_MODE=batch`, rebuild. Verify the existing push-to-talk surface still works unchanged — no regression.
7. Background the app mid-call. Verify audio continues (`UIBackgroundModes` includes `audio`). Foreground again.
8. Lock the device mid-call. Verify call survives.

Capture three Honeycomb permalinks showing the iOS voice turns: `client.kind=ios` filter on `voice.total.mouth_to_ear`, `voice.tts.barge_in_ms`, and `voice.turn.outbound`.

---

## Stop conditions (report, don't work around)

- **Daily Swift SDK has changed API since the docs** — the `CallClient` shape, `sendAppMessage` signature, or callback model may differ from this brief. Adapt and document the version; STOP if the SDK doesn't expose the primitives we need (rare).
- **AVAudioSession conflicts** with Spotify or with an existing recording session — fix the session config; if you can't make `.mixWithOthers` work with the Daily SDK's expectations, STOP and report (we may need to suspend Spotify on voice-mode entry).
- **Native VAD's RMS threshold is too noisy** — try AVAudioSession's `inputGain`, increase the debounce window, or fall back to server-side endpointing (Deepgram's `endpointing=300ms` is already set in orchet-voice; that's the fallback). Document the choice in code comments.
- **Background-audio entitlement requires App Store review** — it's already in `UIBackgroundModes`; if Xcode complains about a missing entitlement on real-device build, fix the entitlement file and document.
- **`xcodebuild test` fails on the existing tests after adding the Daily SPM dep** — the dep import may pull in an Obj-C bridging header that doesn't compile under the project's settings. STOP, report; we may need to add a bridging header or adjust `OTHER_SWIFT_FLAGS`.
- **`Lumo.xcconfig` allow-list rejects the new env vars** — read `scripts/ios-write-xcconfig.sh` and extend its allow-list. If the script enforces a closed set, add `ORCHET_VOICE_MODE` and `ORCHET_VOICE_BASE`.
- **Daily SDK bumps the iOS deployment target above 17.0** — STOP, report. We do not bump the deployment target without a separate review.

---

## What "done" looks like

1. PR ready-for-review on `Orchet-AI/orchet-ios@main`.
2. Three CI commands (xcodegen + xcodebuild build + xcodebuild test) pass.
3. PR body includes:
   - Screenshots of the streaming voice UI + the native confirmation modal.
   - Three Honeycomb permalinks (mouth-to-ear, barge-in, /voice/turn) filtered on `client.kind=ios`.
   - One real-device smoke transcript: a voice booking flight + confirm modal + voice continuation.
   - One side-by-side comparison of streaming vs batch behavior to prove the flag swap is clean.
4. Default flag is `batch`. Production builds continue to use the legacy path until ops flips `ORCHET_VOICE_MODE=streaming` in TestFlight / production builds.
5. After 7 days of TestFlight `ORCHET_VOICE_MODE=streaming` builds with no P1 incident, ops flips the production build's xcconfig and ships an App Store update.

After Phase 6 closes, both web and iOS users hit `orchet-voice.fly.dev`. The gateway `/stt` and `/tts` routes can be retired in a small follow-up PR labelled "PHASE-6-CLEANUP".

---

## References

- [VOICE-ARCHITECTURE-1 ADR v6](../architecture/VOICE-ARCHITECTURE-1.md)
- [Phase 3 brief amendment](./VOICE-PHASE-3-CODEX-BRIEF.md) — the equivalent web cutover, mirror its shape
- [Phase 3 web PR #2](https://github.com/Orchet-AI/orchet-web/pull/2) — reference implementation in TypeScript; many patterns translate directly to Swift
- [Daily Swift SDK](https://github.com/daily-co/daily-client-ios)
- [Daily app messages](https://docs.daily.co/reference/daily-js/instance-methods/send-app-message) — payload shape for `show_confirmation` and `confirmation_resolved` matches between iOS and web
