# Codex brief — VOICE-PHASE-3: Orchestrator integration + visual confirmation

**Brief ID:** VOICE-PHASE-3-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Contract proposal:** [voice-turn-contract-proposal.md](../voice-turn-contract-proposal.md)
**Predecessors:** VOICE-PHASE-1-CODEX + VOICE-PHASE-2-CODEX (both must be merged)
**Status:** Drafted — dispatches after Phase 2 merges
**Owner:** Codex
**Reviewer:** Kalas (CEO/CTO) + Claude
**Estimated effort:** 5 days

This brief is self-contained. Read the parent ADR (link above) for strategic context. Read [voice-turn-contract-proposal.md](../voice-turn-contract-proposal.md) for the full API contract.

---

## Goal

Voice goes from chatbot to real agent. Wire the safety boundary: every LLM-emitted tool call posts to `api.orchet.ai/voice/turn`. The backend orchestrator decides one of three outcomes:

| Outcome | Voice behavior |
|---|---|
| `executed` | Low-risk tool ran on backend; voice continues conversation with the result |
| `requires_visual_confirmation` | Voice says "I've prepared it; please confirm on screen" and emits a `show_confirmation` event over the WebRTC data channel; client renders modal; user taps Confirm; client POSTs to `/voice/confirm-action`; orchestrator commits |
| `denied` | Voice tells user via TTS that this action can't be done by voice; recommend visual app |

This phase spans **three repos** because the contract has both server endpoints and client UI:

- `orchet-voice` — wire the outbound HTTPS round-trip from Pipecat function-call frames
- `orchet-backend` — add the two public gateway routes + orchestrator decision logic + tool registry extension + audit
- `orchet-web` — add the WebRTC data-channel listener + confirmation modal

iOS gets the data-channel listener in Phase 6, not now.

---

## Hard scope boundaries

**You MUST NOT:**
- Add Sarvam (Phase 4)
- Add multi-region deploys (Phase 5)
- Touch iOS code (Phase 6 — WebRTC confirmation modal on iOS is a separate task)
- Execute tools from the voice service directly (the whole point of this phase is that voice does NOT execute)
- Enable irreversible-action tool execution without visual confirmation (the safety boundary is the deliverable)
- Add new LLM/STT/TTS providers (Phase 2's stack is final for Phase 3)

**You MUST:**
- Build `POST /voice/turn` route in orchet-backend (gateway → orchestrator internally)
- Build `POST /voice/confirm-action` route in orchet-backend (gateway → orchestrator internally)
- Extend the tool registry with `voice_allowed`, `requires_visual_confirmation`, `risk_level` flags
- Add audit logging for every `/voice/turn` decision + every `/voice/confirm-action`
- Wire the outbound call from orchet-voice's Pipecat pipeline when LLM emits a function-call frame
- Build the confirmation modal in orchet-web
- Wire the WebRTC data-channel listener on the web client
- Implement the three outcomes correctly: `executed`, `requires_visual_confirmation`, `denied`
- Pass idempotency key on every `/voice/turn` request (UUIDv7)
- Demo a real high-risk flow end-to-end (e.g., "book me a flight to Tokyo tomorrow")

---

## PR structure (three PRs, in this order)

### PR 1 — `orchet-backend`: add voice routes + orchestrator decision logic + tool registry

**Title:** `VOICE-PHASE-3: add /voice/turn and /voice/confirm-action routes + tool risk policy`

**Scope:**
- New gateway route `POST /voice/turn` (per contract proposal § Route 1)
  - Auth: `Bearer <ORCHET_INTERNAL_TOKEN>` (voice service identity, NOT user)
  - User identity comes from request body (`user_id`); voice already validated the user JWT at connection-open
  - Idempotency: cache decisions for 24h keyed on `(user_id, idempotency_key)` in Redis
  - Returns one of three outcomes per the contract
- New gateway route `POST /voice/confirm-action` (per contract proposal § Route 2)
  - Auth: `Bearer <supabase access_token>` (USER auth, not service auth)
  - Validates the `confirmation_id` was issued for this user
  - On `accepted: true`: orchestrator commits the deferred tool call
  - On `accepted: false`: orchestrator records the cancellation; voice service notified via SSE or polling
- Internal route on orchestrator: `POST /orchestrator/voice-decision` — the actual policy + execution logic. Gateway proxies `/voice/turn` to this.
- Tool registry extension: each tool gets these new fields:
  ```yaml
  voice_allowed: true | false        # can this tool be initiated via voice?
  requires_visual_confirmation: true | false  # high-risk?
  risk_level: low | medium | high | critical
  voice_message_pre_confirm: "..."   # what voice says before showing the modal
  voice_denial_message: "..."        # what voice says if denied
  ```
- Default risk classification (must be enforced):
  - Low: search, lookup, compare, read-only → `executed`
  - Medium: send message, draft (not send), set reminder → `executed` with hint
  - High: book, schedule, modify booking, share contact → `requires_visual_confirmation`
  - Critical: charge card, transfer money, sign contract, delete account, legal/compliance → `denied`
- Audit log: write to `audit_log` table (or existing equivalent) for every voice-initiated decision

**Verification:**
- `npm run typecheck` + `npm run test` pass
- New integration tests: one for each of the three outcomes
- One test asserting that critical actions are denied even if `voice_allowed=true` (defense in depth)
- One test asserting idempotency: same `idempotency_key` returns cached decision

**Stop conditions:**
- Redis isn't available in orchet-backend's runtime → use a simple in-memory cache for now and note as Phase 3 followup
- Existing tool registry shape can't accommodate new fields → propose a migration in a separate doc + use a feature-flagged shadow registry first

### PR 2 — `orchet-voice`: wire the outbound `/voice/turn` call

**Title:** `VOICE-PHASE-3: wire /voice/turn outbound from Pipecat function-call frames`

**Scope:**
- When Pipecat's LLM service emits a function-call frame, intercept it (do NOT pass to a tool executor — there is no tool executor in this service)
- POST to `https://api.orchet.ai/voice/turn` (use `ORCHET_GATEWAY_URL` Fly secret, already set)
- Auth: `Bearer <ORCHET_INTERNAL_TOKEN>` (already a Fly secret)
- Generate UUIDv7 idempotency key per turn
- Handle the three response outcomes:
  - `executed`: inject the `result` back into the LLM context, continue the conversation, optionally speak `voice_message_hint`
  - `requires_visual_confirmation`: speak the `voice_message` via TTS, emit `show_confirmation` event over the WebRTC data channel with the `confirmation_payload`
  - `denied`: speak the `voice_message` via TTS, continue the conversation
- Listen on a per-session backend WebSocket (or polling) for confirmation-result events from the orchestrator after the user taps Confirm; speak the `voice_continuation_hint` once received
- New span: `voice.turn.outbound` — records outbound to `/voice/turn` with attributes: `outcome`, `tool_name`, `requires_visual_confirmation`, `latency_ms`

**Verification:**
- `uv run pytest` includes new tests:
  - `test_voice_turn_executed.py`
  - `test_voice_turn_requires_confirmation.py`
  - `test_voice_turn_denied.py`
- Smoke test from web client: "book me a flight to Tokyo tomorrow" triggers the visual-confirmation flow

### PR 3 — `orchet-web`: confirmation modal + data-channel listener

**Title:** `VOICE-PHASE-3: voice-confirmation modal + WebRTC data-channel listener`

**Scope:**
- New component: `<VoiceConfirmationModal>` rendered on a WebRTC `show_confirmation` data-channel event
- Modal shows: title, summary, details (label/value list per contract), Confirm/Cancel buttons
- Confirm → POST `https://api.orchet.ai/voice/confirm-action` with `accepted: true`
- Cancel → POST same route with `accepted: false`
- Expire handler: if `expires_at` passes before user interacts, auto-cancel
- Reuse the existing text-mode visual confirmation styling where possible (DRY)
- WebRTC data-channel wiring lives in `lib/voice-data-channel.ts` (or similar)

**Verification:**
- `npm run typecheck` + `npm run test` + `npm run lint` pass
- New component test asserting:
  - Modal renders with the expected payload shape
  - Confirm calls `/voice/confirm-action` with `accepted: true`
  - Cancel calls with `accepted: false`
  - Auto-cancel on expiry

---

## Cross-PR coordination

Merge order matters:

1. **PR 1 (orchet-backend) first** — establishes the routes the voice service will call
2. **PR 2 (orchet-voice) second** — depends on PR 1's routes existing
3. **PR 3 (orchet-web) last** — depends on PR 1's `/voice/confirm-action` route AND on the data-channel event shape from PR 2

Do NOT merge PR 2 before PR 1. Voice would call into a 404. Use draft PRs until predecessors merge.

---

## High-risk action policy

This phase is the FIRST time voice can initiate real actions on the user's behalf. Get this right:

- **Critical actions are always denied** in voice. Voice never charges cards, never signs contracts, never deletes accounts. The user must use the visual app for these. (Phase 4 may relax this for specific tools after a security review; Phase 3 has no exceptions.)
- **High-risk actions always require visual confirmation.** Voice prepares, voice asks; the client renders a modal; user taps Confirm; the orchestrator commits. The user MUST see what they're agreeing to.
- **Low/Medium risk actions execute directly.** Search, lookup, compare, send-with-default-recipient, set reminder. The orchestrator runs them, returns the result, voice speaks the answer.
- **Audit every voice-initiated decision.** Even `denied` results. Future security audits depend on this.

If during implementation you discover a tool whose risk classification is ambiguous, default to the higher risk tier and note the ambiguity in the PR for human review.

---

## Stop conditions (must report, not work around)

- **Existing `/orchestrator/turn` route already does something similar but for text chat** — confirm the architecture call: separate `/voice/turn` route on the gateway, separate orchestrator handler underneath, but shared business logic in a module both can import. Don't reuse `/orchestrator/turn` directly for voice (different auth, different idempotency, different audit attribution).
- **Pipecat 0.0.61's function-call frame model doesn't surface the `tool_call` payload cleanly** — vendor the relevant Pipecat callback class with attribution; report
- **Redis not available in orchet-backend** — use in-memory + flag as Phase 3 followup tech debt
- **Existing tool registry has no concept of risk level** — add a new field with a default of `medium` and treat missing classification as a YELLOW flag for human review; do NOT silently auto-execute tools without classification
- **WebRTC data channel can't carry JSON > 16KB** — document the limit; the `confirmation_payload` should always fit but flag if a real tool exceeds it
- **The user's session JWT expires mid-confirmation** — refresh client-side or treat as cancel; document the behavior
- **Audit log table doesn't accept the new voice-specific columns** — propose migration in a separate file, gate with a feature flag

---

## Verification checklist (per-PR)

**PR 1 (orchet-backend):**
- [ ] `npm run typecheck --workspaces` passes
- [ ] `npm run test --workspaces` passes
- [ ] Three new integration tests (executed / requires_visual_confirmation / denied)
- [ ] Idempotency test passes
- [ ] Critical-action-denial test passes
- [ ] Audit log assertion in tests
- [ ] No secrets in diff

**PR 2 (orchet-voice):**
- [ ] `uv run ruff check` + `uv run pyright` + `uv run pytest` pass
- [ ] `fly deploy --strategy rolling` succeeds
- [ ] Smoke test: high-risk request triggers visual confirmation flow end-to-end
- [ ] `voice.turn.outbound` span emitted with correct attributes

**PR 3 (orchet-web):**
- [ ] `npm run typecheck` + `npm run test` + `npm run lint` pass
- [ ] Modal renders correctly for all three test payloads (high-risk booking, payment denial, low-risk lookup)
- [ ] Cancel button works
- [ ] Expiry auto-cancel works

---

## What "done" looks like

Phase 3 is complete when:

1. All three PRs merged in order: orchet-backend → orchet-voice → orchet-web
2. End-to-end demo works:
   - Open www.orchet.ai, sign in, click voice mode
   - Say: "Book me a flight to Tokyo tomorrow morning"
   - Voice replies: "I've prepared a JAL flight at 9:30 AM tomorrow, $850. Please confirm on screen."
   - Modal appears with booking details + Confirm/Cancel
   - Tap Confirm
   - Voice continues: "Done. Confirmation code JAL-XYZ123. I've emailed the details."
3. Honeycomb shows `voice.turn.outbound` spans + new audit log entries
4. Low-risk demo also works: "What's the weather in Tokyo?" returns directly without modal
5. Critical-action denial works: "Charge $50 to my credit card" returns voice denial, no modal, no execution
6. Status report posted on tracking issue summarizing all three flows

After Phase 3 closes, voice is a real safety-bounded agent. Phase 4 (Sarvam Indian-language layer) is the next dispatchable lane. Phase 5 (multi-region) follows.

---

## References

- [VOICE-ARCHITECTURE-1 ADR v6](../architecture/VOICE-ARCHITECTURE-1.md)
- [voice-turn-contract-proposal.md](../voice-turn-contract-proposal.md) — full API contract, this brief implements it
- [VOICE-PHASE-2-CODEX-BRIEF.md](./VOICE-PHASE-2-CODEX-BRIEF.md) — predecessor; must be merged first
- [Pipecat 0.0.61 LLM service callbacks](https://github.com/pipecat-ai/pipecat) — for the function-call frame interception
