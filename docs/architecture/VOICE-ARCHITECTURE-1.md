# ADR — Low-latency voice agent architecture (v6)

**ID:** VOICE-ARCHITECTURE-1
**Status:** Approved (v6, 2026-05-10) — ready for execution
**Author:** Kalas (CEO/CTO) + Claude (drafting) + ChatGPT (review)
**Reviewers:** Engineering, Product
**Supersedes:** the current turn-based voice path (gateway → integrations `/stt`/`/tts` + orchestrator `/turn`)

**Version history:**
- **v1** — Modal-hosted voice service. Superseded: (a) Modal's serverless model doesn't fit long-lived WebRTC sessions, (b) voice gateway doesn't run ML so GPU co-location adds nothing.
- **v2** — Moved orchestrator to Fly.io; added Sarvam and Exotel telephony. Superseded: (a) voice cannot own tool execution policy, (b) irreversible actions need visual confirmation, (c) APAC latency measurement must happen earlier, (d) new repo should be split from Day 1.
- **v3** — Locked in safety boundaries, split voice to its own repo, pulled APAC + Sarvam evaluation earlier. Superseded by scope correction: Exotel telephony was scope creep.
- **v4** — Removed Exotel telephony entirely; core voice = browser + iOS WebRTC only. Superseded by phase-decomposition improvement.
- **v5** — Split the original Phase 2 into Phase 2 (pipeline + interruption, no tools) and Phase 3 (orchestrator integration + visual confirmation). Superseded by host-commitment relaxation after third review.
- **v6 (this)** — Softened Fly.io commitment from "approved Phase 1–5" to "Phase 1 pilot, validated at Phase 1b India RTT probe, fallback hosts documented". Added explicit decision-comparison section showing why Pipecat + Daily + Fly.io were chosen over alternatives and what triggers a pivot. Pipecat commitment stays locked (replaceable later at defined cost). Phase 0 scope unchanged — measure existing path, no 3-way pilot bake-off.

---

## TL;DR

We build a dedicated **voice service** (new repo: `Orchet-AI/orchet-voice`) as a **pilot on Fly.io**, with multi-region rollout planned after Phase 1b India RTT probe validates the host choice. The service terminates WebRTC sessions from the web app and iOS app. It runs **Pipecat OSS** which streams audio through **Deepgram or Sarvam** (language-aware STT), a **per-agent LLM router** (Groq fast path / Claude quality path), and back through **Deepgram Aura-2 or Sarvam Bulbul** TTS.

> **Note on numbers:** All cost and latency estimates in this ADR are planning assumptions. They become facts only when Phase 0 measurement and Phase 1 pilot validate them. Treat tables as targets to verify, not commitments.

**Scope (explicit):** Voice mode is for users talking to the Orchet super-agent through the web app and iOS app. This ADR is NOT about telephony — users calling a phone number is a separate product surface with separate requirements (business phone numbers, compliance, μ-law audio, caller-ID identity). If we ever pursue telephony, it gets its own ADR (VOICE-TELEPHONY-1 or similar).

**Safety boundaries (hard):**
- Voice service does NOT execute tools directly. All tool dispatch flows through `api.orchet.ai/voice/turn`, where the backend gateway routes to the orchestrator. The orchestrator owns tool policy, permissions, confirmation requirements, and audit logging.
- Irreversible actions (payment, booking commit, cancel-paid-booking, send-irreversible-message, legal/compliance) ALWAYS require visual confirmation. Voice prepares + asks; client confirms on screen.

**Where Modal fits:** ML brain only (`lumo-ml-service` — embeddings, classification, BGE, CLIP, Whisper batch). Modal is NOT in the audio hot path.

**Hosting commitment:** Fly.io is the **Phase 1 pilot** host. Validated by the Phase 1b India RTT probe (target p50 < 200 ms from a Mumbai-region client). If the probe fails the gate, we pivot to a fallback host per the [Decision comparison](#decision-comparison) section. Re-validated at Phase 6 production-hardening review under launch traffic.

**Repo:** New `Orchet-AI/orchet-voice` repo (matches established split pattern with mcp / web / ios / android / brand / backend).

**Target latencies (mouth-to-ear response, p50 / p95):**

| User region | Phase 1 (Fly IAD = production; BOM = probe only) | Phase 5 (multi-region production) |
|---|---|---|
| US East | 700 ms / 1.1 s | 700 ms / 1.1 s |
| EU | 950 ms / 1.4 s | 750 ms / 1.2 s |
| India / SE Asia | 1.2 s / 1.8 s ⚠️ | 800 ms / 1.3 s |

**Build effort:** 7 numbered phases (Phase 0 through Phase 6). Implementation Phases 1–5 take ~5 weeks. Phase 0 is 3 days of measurement before Phase 1 starts. Phase 6 is ongoing post-launch hardening. Phase 2 (web voice pipeline, no tools yet) is demoable ~2 weeks in.

---

## Why this matters

Voice is the product differentiator. Text chat is commoditized. Voice that *feels real* — sub-1-second response, interruptible, no awkward silence — separates products people use daily from products people demo once.

The latency budget for "natural conversation" is well-established:

- **Under 800 ms p50** — feels natural
- **800 ms – 1.2 s** — noticeable lag but tolerable
- **1.2 s – 1.8 s** — users start interrupting / talking over the agent
- **1.8 s+** — feels broken

The current REST-based path (record-upload-transcribe-prompt-stream-synthesize-play) burns 3–6 seconds per turn. Demo path, not a product path.

---

## Latency budget breakdown

| Stage | Best | Typical | What we control |
|---|---|---|---|
| Client-side VAD endpointing | 100 ms | 200–300 ms | Silero VAD model + tuning |
| Client → Fly gateway RTT (in-region user) | 25 ms | 40 ms | Region choice |
| Client → Fly gateway RTT (out-of-region) | 150 ms | 220 ms | Multi-region (Phase 5) |
| STT first partial (Deepgram Nova-3, streaming) | 80 ms | 150 ms | Streaming, not batch |
| STT first partial (Sarvam Saarika, streaming) | 100 ms | 200 ms | Streaming |
| LLM TTFT (Groq Llama 3.3 70B) | 100 ms | 200 ms | Provider + prompt size |
| LLM TTFT (Claude Sonnet) | 350 ms | 600 ms | Provider + prompt size |
| TTS first chunk (Deepgram Aura-2, WebSocket stream) | 80 ms | 150 ms | Streaming, sentence boundaries |
| TTS first chunk (Sarvam Bulbul) | 100 ms | 200 ms | Streaming |
| Gateway → Client return | 25 ms | 40 ms | Same as outbound |
| Audio buffer + playback | 30 ms | 50 ms | Client implementation |

**Two non-obvious points:**

1. **Pipelining is mandatory.** STT, LLM, TTS overlap — STT emits partials → LLM starts generating on the partial → TTS starts speaking on the first sentence. Sequential adds 300–600 ms even when each stage is fast.

2. **LLM TTFT dominates.** Per-agent LLM router lets quote-aware booking flows pay for Claude Sonnet's quality; quick chat answers use Groq.

---

## Why a dedicated voice gateway, not Modal

(This is the most important architectural decision in the ADR. The v1 draft put voice on Modal; that was wrong.)

### What lives where

| Concern | Right home | Why |
|---|---|---|
| **WebRTC session termination** (long-lived, stateful, audio media) | **Fly.io always-on** | Fly's Machines are designed for long-running stateful services. Modal's serverless model fights this. |
| **Pipecat orchestration loop** (streaming, real-time) | **Fly.io always-on** | Same reason. Modal `min_containers=1` works but isn't architecturally native. |
| **STT / LLM / TTS calls** | External provider APIs | We don't self-host these models. No GPU co-location benefit. |
| **Heavy ML brain** (embeddings, classification, BGE/CLIP/Whisper) | **Modal** | Modal's whole reason for existing. Stays at `lumo-ml-service`. |

### Why not Modal for the voice gateway

Modal's strengths are serverless GPU inference and burst scaling. Its `min_containers=1` keeps a container warm, but:

- Modal's mental model is request-response, not persistent session
- Cold-start tax under burst is real (3–8 s) even with min_containers=1
- WebSocket support is newer than dedicated voice infra
- Cost: ~$0.50/hour per warm container × 3 regions × 24 × 30 = ~$1,170/mo just for warm capacity
- **The voice gateway doesn't run ML** — it orchestrates calls to external APIs. Modal's GPU co-location adds nothing.

### Why not Render

- Render Pro starts at $25/mo per service and only has US + EU regions
- No India / APAC region — kills our latency targets for the largest growth market
- Otherwise comparable to Fly

### Why Fly.io specifically

- **30+ regions** including Mumbai (bom), Singapore (sin), Frankfurt (fra), Sao Paulo (gru), Sydney (syd)
- Fly Machines start at $5/mo per machine, region-independent scaling
- Mature WebSocket and persistent-connection support
- Pipecat-compatible (Docker-based runtime)
- Per-region cost: ~$30/mo (1 always-on machine, 2 GB) × 3 regions = ~$90/mo baseline (vs Modal $1,170/mo)
- **~$1,000/mo savings** vs the v1 Modal proposal

### Why not UI / client-side

The client (web + iOS) does what it's good at:
- Mic capture (16 kHz mono PCM)
- Client-side VAD (Silero WASM)
- WebRTC connection management
- Audio playback

The client must NOT own:
- STT/TTS provider API keys
- Conversation state
- Tool dispatch
- User identity
- Memory / context
- Cross-agent routing
- Fallback / retry logic

Anything sensitive or stateful is server-side.

---

## The stack

```
┌────────────────────────────────────────────────────────────────────┐
│                  Client (Web Next.js / iOS / Android)               │
│   Mic capture → Silero VAD → WebRTC → wss://voice.orchet.ai         │
│   Bearer JWT in signaling                                           │
└────────────────────────────────────┬───────────────────────────────┘
                                     │ Opus 20 ms over WebRTC
                                     │ (Daily handles SFU + regions)
                                     ▼
┌────────────────────────────────────────────────────────────────────┐
│           orchet-voice (Fly.io, 4 regions: bom/sin/iad/fra)         │
│                                                                    │
│   Pipecat pipeline (Python, FastAPI control plane):                │
│                                                                    │
│   ┌──────────┐  ┌───────────────────┐  ┌─────────┐  ┌──────────┐   │
│   │  Daily   │→ │  Language router  │→ │   LLM   │→ │ TTS via  │   │
│   │ WebRTC   │  │  → Deepgram (EN)  │  │ router  │  │ Aura-2   │   │
│   │ adapter  │  │  → Sarvam (IN)    │  │ Groq /  │  │ or Bulbul│   │
│   │          │  │  streaming STT    │  │ Claude  │  │ (lang)   │   │
│   └──────────┘  └────────┬──────────┘  └────┬────┘  └──────────┘   │
│                          │                  │                      │
│                          │ partial          │ function-call frames │
│                          │ transcript       │                      │
│                          ▼                  ▼                      │
│            ┌─────────────────────────────────────────────┐         │
│            │  Outbound HTTPS to api.orchet.ai (gateway)  │         │
│            │  POST /voice/turn          (tool decisions) │         │
│            │  POST /sessions/{id}/messages (transcript)  │         │
│            │                                             │         │
│            │  (/voice/confirm-action is called by the    │         │
│            │   CLIENT after user taps confirm — never    │         │
│            │   by the voice service directly)            │         │
│            └─────────────────────────────────────────────┘         │
│                                                                    │
│   JWT validation at WS open. Internal token to call back to        │
│   gateway. Honeycomb tracing.                                      │
└────────────────────────────────────────────────────────────────────┘
                                     │
                                     ▼ async, off the hot path
┌────────────────────────────────────────────────────────────────────┐
│           api.orchet.ai gateway → backend services                 │
│   Tool routing, conversation persistence, agent registry            │
│   (Modal lumo-ml-service for embeddings, classification, etc.)      │
└────────────────────────────────────────────────────────────────────┘
```

### Component choices

**Transport — WebRTC only (browser + iOS).**
WebRTC is required for Day-1 interruption. UDP-based jitter buffer + packet loss concealment handles the realities of mobile network. Pipecat's co-founder Kwindla Hultman-Kramer: *"WebRTC runs on UDP, was built for low-latency real-time media, handles NAT traversal, and produces noticeably better interruption handling and voice quality than WebSocket transport."* No telephony / Exotel adapter in this ADR — telephony is out of scope (separate product surface, separate ADR if pursued).

**WebRTC transport adapter — Daily Cloud.**
$0.01/min compute. By Pipecat's creators — best-supported. Handles global SFU, NAT traversal, region routing. Migration path to self-hosted SFU (mediasoup or LiveKit on Fly) at ~5k voice hours/month if cost drives it.

**Orchestration — Pipecat OSS.**
Pipeline-first framework, exactly fits our streaming STT→LLM→TTS shape. LiveKit Agents is the alternative; LiveKit shines for multi-party voice/video rooms (5 humans + AI), which is not our use case. Pipecat for single-user agent voice; LiveKit revisit if we add multi-party features.

**STT — Deepgram Nova-3 (English/EU) + Sarvam Saarika (Indian languages).**
Language detection on first partial; route to the right provider. Sarvam handles 22 Indian languages including Telugu, Tamil, Marathi, Bengali, Gujarati, Kannada — Deepgram coverage is patchy here. API key for Deepgram already in `lumo-ml-service` Modal Secret; Sarvam key gets added in Phase 3.

**LLM router — per-agent.**
- Fast default: **Groq Llama 3.3 70B** (~150 ms TTFT, $0.59/M input)
- Quality: **Claude Sonnet** (~500 ms TTFT, $3/M input)
- Specialty: **OpenAI Realtime** if we ever need integrated STT+LLM (unlikely)
- Agent manifest declares preferred model; voice service reads the same registry as text chat

**TTS — Deepgram Aura-2 (English/EU) + Sarvam Bulbul (Indian).**
Aura-2: ~100 ms first chunk, $0.015/M chars. Bulbul: native Indian-language voices. Both stream over WebSocket — sentence-boundary chunking so playback starts on the first complete sentence.

**Auth — Bearer JWT at WebRTC signaling.**
Same pattern as the web gateway we just shipped. Web client reads `supabase.auth.getSession().access_token` and includes it in the WebRTC signaling exchange. Voice service validates against Supabase on connection-open; trusts session for the WebRTC lifetime. iOS uses the same flow. Anonymous voice is not supported in this scope — users must be signed in (the agent has tools that act on their behalf; we need an identity).

**Tool calls — round-trip via `api.orchet.ai/voice/turn`.**
Voice service does NOT execute tools. When the LLM emits a function-call frame, voice POSTs to `api.orchet.ai/voice/turn` with the internal service-to-service token. The backend gateway routes this internally to the orchestrator, which decides: `executed` (orchestrator ran the tool, returns result), `requires_visual_confirmation` (voice prompts user, client shows modal), or `denied` (policy block). Same auth surface as text chat — voice doesn't get its own backdoor for tool execution.

**Persistence — async background writes.**
Every turn's transcript + tool calls POST to `api.orchet.ai/sessions/{id}/messages` off the critical path. Failed persistence doesn't stall voice.

---

## Phased plan (Phase 0 through Phase 6; implementation Phases 1–5 ≈ 5 weeks)

> **Reading guide:** Phase 0 is 3 days of measurement before any code. Phases 1–5 are the ~5-week build. Phase 6 is post-launch and ongoing — it runs in parallel with normal product work after the v1 launch.

### Phase 0 — Measure + evaluate (3 days)

**Measurement:**
- Instrument existing REST flow with OpenTelemetry spans: `voice.client.capture`, `voice.upload`, `voice.stt.batch`, `voice.orchestrator.turn`, `voice.tts.batch`, `voice.client.play`
- Honeycomb dashboard with p50/p95/p99 per stage
- Establish baseline numbers we'll improve against

**Sarvam evaluation (pulled from v2 Phase 3):**
- Sign up for Sarvam developer account
- Price discovery — confirm $0.50/hour Saarika tier is current
- Quality smoke test — 5 sentences each in Telugu, Hindi, Tamil through Saarika STT + Bulbul TTS
- Test code-mixing (Hinglish) explicitly — biggest unknown for India market

**Output:** Baseline latency dashboard + Sarvam go/no-go decision.

### Phase 1 — `Orchet-AI/orchet-voice` repo + skeleton (5 days)

**1a. Repo scaffold (1 day):**
- Create `Orchet-AI/orchet-voice` repo (matches mcp/ios/android/brand/backend/web pattern)
- Python project skeleton: FastAPI control plane + Pipecat pipeline shell
- Dockerfile for Fly.io deployment
- Fly.io app provisioned, secrets configured (Deepgram, JWT, internal token)
- CI: GitHub Actions for lint + typecheck + test

**1b. US East skeleton (2 days):**
- Daily WebRTC transport adapter wired
- Bearer JWT validation on signaling connect
- Echo pipeline (audio in → audio out, no STT/LLM/TTS yet)
- Deploy to Fly iad (Ashburn) single region
- Honeycomb tracing wired
- Health endpoint
- Smoke test from a vanilla web client (Daily JS SDK)

**1c. India latency probe (2 days):**
- Deploy a second Fly Machine to bom (Mumbai)
- Measure WebRTC connection RTT from a real Indian network endpoint
- Decision tree:
  - p50 RTT < 200 ms → APAC region stays at Phase 5 (multi-region rollout)
  - p50 RTT 200–400 ms → APAC promotes ahead of Sarvam / multi-region scheduling
  - p50 RTT > 400 ms → APAC blocker, promotes to Phase 2

**Output:** `wss://voice.orchet.ai` reachable in US East + bom, baseline RTT measured both regions, APAC scheduling decision made.

### Phase 2 — Streaming pipeline + interruption (5 days)

**Scope:** Prove the voice infrastructure works end-to-end. NO tool execution yet — the agent answers from LLM general knowledge only ("what's the weather in Tokyo?", "explain quantum computing"). Tools come in Phase 3.

**Pipeline:**
- Deepgram STT (streaming, interim results on)
- Groq Llama 3.3 70B (streaming, no tool/function calling enabled in this phase)
- Deepgram Aura-2 TTS (streaming, sentence-level chunking)
- Client-side Silero VAD (browser WASM)
- Pipecat barge-in handler — when client VAD fires mid-agent-response, cancel TTS + restart listening

**Background:**
- Transcript persistence to `api.orchet.ai/sessions/{id}/messages` (async, off hot path)
- Latency telemetry per stage to Honeycomb

**Demo:** Open voice mode in the web app, ask "what time is it in Tokyo?", hear answer within ~1 second of finishing speaking. Interrupt mid-answer with "actually, what about London?" — agent stops, listens, answers.

**Output:** Voice infrastructure validated. Mouth-to-ear p50 < 900 ms for US users. Interruption working. Latency dashboards live. No tool execution yet — agent can't book flights or take actions.

### Phase 3 — Backend orchestration + visual confirmation (5 days)

**Scope:** Voice becomes a real agent. Wire tool execution through the orchestrator, with safety boundary intact.

**Safety boundary (the central change):**
- Voice service does NOT execute tools directly. When the LLM emits a function-call frame in Phase 3's pipeline, voice POSTs to:
  ```
  POST api.orchet.ai/voice/turn
  Authorization: Bearer <internal-service-token>
  {
    "session_id": "voice_abc123",
    "user_id": "<from JWT>",
    "tool_call": { "name": "...", "arguments": {...} },
    "channel": "voice",
    "locale": "en-US"
  }
  ```
- The gateway routes `/voice/turn` internally to the orchestrator, which returns one of three outcomes:
  - `executed` — orchestrator ran the tool itself (low-risk: search, lookup, compare), returns result, voice continues conversation
  - `requires_visual_confirmation` — voice service says via TTS: "I've prepared it; please confirm on screen" AND emits a `show_confirmation` event over the WebRTC data channel; client renders confirmation modal; user taps; modal POSTs to `api.orchet.ai/voice/confirm-action` to commit
  - `denied` — tool blocked by policy (e.g., voice never allowed to trigger payments without explicit confirmation); voice tells user via TTS

**Tool metadata contract (added to existing agent manifest schema):**
```json
{
  "id": "book_flight_confirm",
  "requires_visual_confirmation": true,
  "voice_allowed": true,
  "voice_message_pre_confirm": "I've prepared your flight booking. Please confirm on screen."
}
```

**Backend additions (in `orchet-backend`):**
- New public gateway route `POST /voice/turn` — receives tool-dispatch requests from voice service, routes internally to orchestrator
- New public gateway route `POST /voice/confirm-action` — receives client confirmation taps after a `requires_visual_confirmation` outcome
- Tool registry extension — every tool gets `voice_allowed` + `requires_visual_confirmation` flags
- Audit log: every voice-initiated tool call recorded with confirmation outcome

**Client additions (web first, iOS later in Phase 6):**
- WebRTC data channel listener for `show_confirmation` events
- Confirmation modal component (reuse existing visual confirmation UI from text chat where possible)
- "Confirm" → POST to `api.orchet.ai/voice/confirm-action` → orchestrator commits the tool call

**Demo:** Open voice mode, say "book me a flight to Tokyo tomorrow". Agent: "I've prepared a flight from SFO to NRT tomorrow at 10:30 AM, $850. Please confirm on screen." Web app shows a confirmation modal with the booking details + Confirm/Cancel buttons. Tap Confirm → booking commits → agent: "Done. Confirmation email sent."

**Output:** Voice is a real agent with safety boundary. Irreversible actions always route through visual confirmation. Tool registry expanded with voice-aware metadata.

### Phase 4 — Sarvam Indian-language layer (4 days)

- Language detection on STT input (Pipecat language-detection processor)
- Router: English/EU → Deepgram; Indian languages → Sarvam Saarika
- Sarvam Bulbul TTS for matching output language
- Code-mixing handling (Hinglish — Hindi + English in same sentence — common in India)
- Add Sarvam API key to Fly secrets

**Output:** Native Indian-language voice support. Demo: speak Telugu in the web app voice mode → agent responds in Telugu.

### Phase 5 — Multi-region + per-agent LLM router (5 days)

- Fly.io machines in bom (Mumbai), sin (Singapore), fra (Frankfurt), iad (US East)
- Daily handles SFU region routing automatically
- DNS: voice.orchet.ai → Fly anycast / fly.dev routing
- Per-agent LLM router: agent manifest declares Groq | Claude | OpenAI
- Cost telemetry: $/voice-minute per agent

**Output:** Voice latency p50 < 900 ms in all three primary regions. Cost dashboards.

### Phase 6 — Production hardening (ongoing post-launch)

- iOS WebRTC native client (Daily iOS SDK)
- Voice eval framework (Cekura or in-house simulation)
- Load testing (100 concurrent sessions/region)
- Failover: Daily → SmallWebRTCTransport (pinned <0.0.62)
- Recording + consent flow
- PII redaction in transcripts
- Tool confirmation policy enforcement audit (every quarter)

---

## Cost projections

### Always-on compute

| Service | Regions | Cost |
|---|---|---|
| Fly Machines (orchestrator) | 4 (bom/sin/fra/iad) | ~$120/mo |
| Daily WebRTC compute | global | $0.01/min |
| **Baseline at Phase 5** | | **~$120/mo + variable** |

vs my v1 (Modal): **~$1,170/mo** at 3 regions. **Net savings: ~$1,050/mo** by switching to Fly.

### Per 10-minute conversation

| Stack | STT | LLM | TTS | Daily | **Total** |
|---|---|---|---|---|---|
| Groq fast path | $0.43 | $0.04 | $0.05 | $0.10 | **$0.62** |
| Claude quality | $0.43 | $0.35 | $0.05 | $0.10 | **$0.93** |
| Sarvam (India) | $0.30 | $0.04 | $0.06 | $0.10 | **$0.50** |
| **Comparison: OpenAI Realtime** | — | — | — | — | **$3.00** |

Mix-per-agent at scale saves ~5× vs OpenAI Realtime end-to-end.

---

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Fly.io machine restarts mid-call | Low | High | Pipecat session state replicated to Redis; reconnect from client picks up. |
| Daily WebRTC outage | Low | High | Phase 6 failover: self-hosted SmallWebRTCTransport. |
| Sarvam latency > Deepgram | Medium | Medium | Document per-language latency; user-facing language choice. |
| LLM TTFT spikes (esp. Claude) | High | Medium | Acknowledgment filler ("mm-hmm") if TTFT >400 ms detected. |
| Hinglish code-mixing breaks STT | Medium | Medium | Sarvam claims code-mixing support; test with real users in Phase 3. |
| Cross-region cookie / JWT issues | Low | Low | Voice service uses Bearer-only auth, no cookies. Already solved. |

---

## Open decisions (need answers before Phase 4 / Phase 5)

1. **Sarvam pricing tier** — depends on monthly volume. Starts at $0.50/hour Saarika STT. Action: signup + estimate in Phase 0.
2. **Daily vs self-hosted SFU long-term** — defer to Phase 5 load test. Sticky on Daily until cost or feature constraint forces migration.
3. **Voice-specific system prompts** — likely shorter, more conversational than text-mode prompts. Spec out in Phase 2.
4. **Recording + retention** — opt-in or default-on? Phase 5 legal review.

---

## Out of scope (explicit)

- **Telephony / PSTN** — users dialing a phone number to reach the agent is a separate product surface. Different audio format (8 kHz μ-law), different identity model (caller-ID), different compliance (TRAI India / FCC US / etc.), different pricing model. If pursued, gets its own ADR (VOICE-TELEPHONY-1 or similar).
- **Anonymous voice** — voice users must be signed in. The agent has tools that act on their behalf; an identity is required.
- **Real-time translation** — same architecture supports it; ship monolingual mix first.
- **Video calls** — 10× complexity; not a 2026 problem.
- **Voice cloning for the agent persona** — Aura-2 / Bulbul defaults are fine.
- **On-device LLM for offline mode** — interesting research, not a product priority.
- **WhatsApp voice notes** — interesting; defer.

---

## Decision

Approve the v6 architecture. Start Phase 0 (measurement + Sarvam evaluation) this week. Phase 1 (`Orchet-AI/orchet-voice` repo + skeleton + India latency probe) ships next week.

**Hard requirements locked in:**
1. New repo `Orchet-AI/orchet-voice` (matches established split pattern)
2. Voice service does NOT execute tools — orchestrator owns tool policy
3. Irreversible actions ALWAYS require visual confirmation
4. India latency measured in Phase 1; APAC schedule decision is data-driven
5. Sarvam evaluated in Phase 0; integrated in Phase 3
6. Voice channel is browser + iOS WebRTC ONLY; telephony out of scope
7. Voice requires authenticated users; anonymous voice not supported
8. Fly.io host is a pilot decision, revisited at Phase 5 production-hardening review

**v4 changes vs v3:**
- Removed Exotel telephony entirely (separate product surface, out of scope)
- Removed phone-call architecture branch from diagram
- Removed phone-number → user_id mapping from Phase 4
- Renumbered remaining phases (telephony Phase 4 deleted; multi-region was Phase 5, now Phase 4; hardening was Phase 6, now Phase 5)
- Total phases: 7 (Phase 0 + Phases 1–5 build + Phase 6 ongoing); Phases 1–5 ≈ 5 weeks (was 6.5 weeks with telephony)
- Cost: unchanged for non-telephony stack (~$120/mo baseline + variable)

**v3 changes vs v2:**
- Repo placement: `Orchet-AI/orchet-voice` from Day 1 (was: inside backend)
- Tool execution: voice → orchestrator → tool (was: voice → tool directly)
- Visual confirmation: hard requirement baked into Phase 2
- APAC measurement: Phase 1b (was: Phase 5)
- Sarvam evaluation: Phase 0 (was: Phase 3 integration only)

**v2 changes vs v1:**
- Moved orchestrator from Modal → Fly.io (always-on service is native there; no GPU co-location need)
- Added Sarvam (Indian-language coverage for browser/mobile voice)
- Moved interruption from Phase 3 → Phase 2 (Day-1 must-have)
- Cost dropped from ~$1,170/mo → ~$120/mo baseline by changing host

---

## Decision comparison

This section documents *why* the locked choices won and *what would trigger a pivot*. It is the audit trail behind v6 — a third reviewer asked for this explicitly.

### Pipeline framework — locked: Pipecat

| Option | Considered | Why not chosen | Trigger to revisit |
|---|---|---|---|
| **Pipecat OSS** | ✓ Chosen | Pipeline-first; fits single-user agent voice; mature; creators support Daily transport | — |
| LiveKit Agents | ✓ Evaluated | Infra-first; shines for multi-party rooms (5 humans + AI); our use case is one user + one agent — Pipecat's pipeline composability wins | We add multi-party features (voice agent joins a Zoom-style call) |
| Custom minimal WebRTC bridge | ✓ Evaluated | Reinvents pipeline, VAD, barge-in, frame handling — we'd burn weeks rebuilding what Pipecat provides | We hit a Pipecat-specific blocker that can't be patched upstream |
| Daily Bots / Pipecat Cloud (managed) | ✓ Evaluated | Less control, vendor lock-in, scales with pricing markup; for a critical-path product service we prefer self-host | If team velocity is the constraint and infra burn is unaffordable at <$5k/mo voice spend |

**Pivot cost if Pipecat ever becomes wrong:** ~1 week to swap framework. Pipeline shape (transport → STT → LLM → TTS + VAD + interruption) is framework-agnostic. Provider clients (Deepgram, Groq, Aura-2) carry over as-is.

### Host — Phase 1 pilot: Fly.io

| Option | Considered | Decision | Trigger to pivot |
|---|---|---|---|
| **Fly.io Machines** | ✓ Pilot | Best fit for long-lived stateful WebRTC sessions; 30+ regions including bom (Mumbai), sin (Singapore); $30/mo/region; mature WebSocket support | Phase 1b India RTT probe shows p50 > 400 ms → pivot to fallback A; OR ops complexity proves untenable → pivot to fallback B |
| Render Pro | Considered | US/EU only — no India region; misses our largest growth market | (used as **fallback A** for US/EU-only fallback if Fly fails India) |
| LiveKit Cloud (managed) | Considered | Managed media + agent infra; pricing scales worse at high voice-hour volumes; vendor lock-in | (used as **fallback B** for managed-everything if ops bandwidth is the constraint) |
| Railway | Considered | Less mature than Fly for WebSocket-heavy workloads; smaller region coverage | — |
| AWS ECS/Fargate | Considered | Full control but high ops overhead; only sensible at $5k+/mo voice spend | (used as **fallback C** for max control if we hit $10k+/mo voice scale and migrate off PaaS) |
| Modal | Considered | Serverless model fights long-lived sessions; cold-start tax; cost higher than Fly at warm capacity | Reserved for ML brain (`lumo-ml-service`), not for voice |

**Fallback decision tree (executed at end of Phase 1b):**

| Phase 1b probe result | Action |
|---|---|
| India p50 < 200 ms, no ops blockers | Continue with Fly for Phases 2–5 |
| India p50 200–400 ms | Continue with Fly; prioritize multi-region deploy earlier (move from Phase 5 to Phase 3) |
| India p50 > 400 ms | **Pivot** — evaluate LiveKit Cloud (fallback B) for managed multi-region, or Render Pro (fallback A) for US/EU launch with India deferred |
| Ops complexity > 2 days/week | **Pivot** — LiveKit Cloud (fallback B) |
| Voice cost > $5k/mo with growth ahead | **Phase 5 review** — AWS ECS (fallback C) for sustained scale |

**Pivot cost if Fly ever becomes wrong:** ~3 days to migrate (Docker portable; Pipecat portable; env vars portable). Daily transport, providers, and `/voice/turn` contract all carry over unchanged.

### WebRTC transport — locked: Daily Cloud

| Option | Considered | Why not chosen | Trigger to revisit |
|---|---|---|---|
| **Daily Cloud** | ✓ Chosen | By Pipecat creators; best-supported transport adapter; $0.01/min; global SFU built-in; handles NAT/region routing | — |
| Self-hosted SFU (mediasoup, LiveKit, Janus) | ✓ Evaluated | Multi-week project on its own; not justified until 5k+ voice hours/mo | Daily costs scale linearly past $2k/mo voice transport spend |
| SmallWebRTCTransport (Pipecat built-in) | ✓ Evaluated | v0.0.62+ has known choppy-audio regression; pin to 0.0.61 if used | Used as Phase 6 failover only |

---

## References

- [Modal blog — One-Second Voice-to-Voice Latency with Modal, Pipecat, and Open Models](https://modal.com/blog/low-latency-voice-bot) (Modal's claim; we adopt the Pipecat part, decline the host part)
- [Pipecat — open-source voice agent framework](https://github.com/pipecat-ai/pipecat)
- [Pipecat Voice Agent in Production — Optimization & Architecture Guide](https://luonghongthuan.com/en/blog/pipecat-voice-agent-production-scalable-guide/)
- [Daily.co — "You don't need a WebRTC server for your voice agents"](https://www.daily.co/blog/you-dont-need-a-webrtc-server-for-your-voice-agents/)
- [WebRTC.ventures — Voice AI framework comparison (2026)](https://webrtc.ventures/2026/03/choosing-a-voice-ai-agent-production-framework/)
- [Deepgram — Measuring Streaming Latency](https://developers.deepgram.com/docs/measuring-streaming-latency)
- [Deepgram TTS WebSocket](https://developers.deepgram.com/docs/tts-websocket)
- [Sarvam AI — Indian-language speech platform](https://www.sarvam.ai/)
- [Fly.io regions and Machines](https://fly.io/docs/reference/regions/)
- [Cekura — Testing Pipecat voice agents](https://www.cekura.ai/blogs/test-pipecat-voice-agents)
- [LiveKit vs Pipecat — Real Differences](https://www.cekura.ai/blogs/pipecat-vs-livekit-the-real-difference)

### Out-of-scope references (for a future telephony ADR if pursued)

These links are intentionally kept here as a future-research seed, NOT because telephony is in scope for VOICE-ARCHITECTURE-1.

- [Exotel AgentStream — bidirectional voice streaming](https://docs.exotel.com/exotel-agentstream/bidirectional-streaming) (India PSTN)
