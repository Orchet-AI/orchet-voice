# Codex brief — VOICE-PHASE-2: Streaming STT + LLM + TTS pipeline + interruption

**Brief ID:** VOICE-PHASE-2-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Predecessor:** VOICE-PHASE-1-CODEX (must be merged before this dispatches)
**Status:** Drafted — dispatches after Phase 1 merges
**Owner:** Codex
**Reviewer:** Kalas (CEO/CTO) + Claude
**Estimated effort:** 5 days

This brief is self-contained. Read the parent ADR (link above) for strategic context.

---

## Goal

Replace Phase 1's echo processor with a real streaming voice pipeline: Deepgram STT (streaming, interim results) → Groq Llama 3.3 70B (streaming) → Deepgram Aura-2 TTS (streaming, sentence-level chunking). Add client-side Silero VAD + Pipecat barge-in so the user can interrupt the agent mid-sentence. Demo at end of phase: voice question, voice answer, interruption all working.

**You are NOT enabling tool calls in Phase 2.** The LLM answers from general knowledge only. Tool calling + the `/voice/turn` safety boundary is Phase 3. Don't enable Pipecat function-calling, don't POST to `api.orchet.ai/voice/turn`, don't add any tool registry.

---

## Hard scope boundaries

**You MUST NOT:**
- Wire any tool execution or function calling (Phase 3)
- POST to `api.orchet.ai/voice/turn` or `/voice/confirm-action` (Phase 3 routes don't exist yet)
- Add Sarvam (Phase 4)
- Multi-region deploy beyond what Phase 1 already has (Phase 5)
- iOS WebRTC client work (Phase 6)
- Touch `orchet-backend`, `orchet-web`, or `orchet-ios` (this PR is `orchet-voice` only)
- Add provider keys to the repo — they're already Fly secrets on orchet-voice
- Change WebRTC transport (Daily stays — proved out in Phase 1)
- Upgrade Pipecat past 0.0.61

**You MUST:**
- Replace echo with Deepgram STT (streaming, `interim_results=true`)
- Wire Groq Llama 3.3 70B as the LLM (streaming response)
- Wire Deepgram Aura-2 as TTS (streaming, sentence-level chunking — first audio chunk on first complete sentence from LLM)
- Add Silero VAD on the web smoke-test client (WASM)
- Add Pipecat barge-in: when user starts speaking mid-agent-response, cancel TTS, restart listening
- Emit spans per the ADR Phase 0 baseline: `voice.stt.batch`, `voice.llm.stream`, `voice.tts.stream` (new names for the streaming variants; document the rename if you keep names different)
- Transcript persistence: async POST to `api.orchet.ai/sessions/{session_id}/messages` off the hot path (use the same `ORCHET_GATEWAY_URL` + `ORCHET_INTERNAL_TOKEN` Fly secrets already set)
- Demo: open the smoke-test page, ask "what time is it in Tokyo?", get a spoken answer in < 1 second after finishing speaking, then interrupt with "actually, what about London?" — agent stops mid-sentence and answers London

---

## Span naming — important

Phase 0 instrumented the **batch REST** voice path with span names like `voice.stt.batch`, `voice.tts.batch`. Phase 2's pipeline is **streaming**, not batch. To avoid name collision in Honeycomb, use these new span names:

| Pipeline stage | Phase 0 span (batch) | Phase 2 span (streaming) |
|---|---|---|
| STT | `voice.stt.batch` | **`voice.stt.stream`** |
| LLM | `voice.orchestrator.turn` | **`voice.llm.stream`** (new — Phase 0 had no LLM span) |
| TTS | `voice.tts.batch` | **`voice.tts.stream`** |
| End-to-end | `voice.total.mouth_to_ear` | **`voice.total.mouth_to_ear`** (same — that's the user-perceived metric, shared across paths) |

The Phase 0 dashboard's "Per-stage p50" panel will pick up the new spans automatically because its filter is `name starts-with "voice."`. The "End-to-end" panel will continue to compare batch vs streaming via `voice.total.mouth_to_ear`. That's intentional.

Attributes per span (in addition to the global `voice.session_id`, `voice.turn_id`, `client.kind` already in place from Phase 0/1):

- `voice.stt.stream`: `voice.stt.first_partial_ms` (time to first interim transcript), `voice.stt.final_ms` (time to final transcript after endpoint), `voice.stt.partial_count`
- `voice.llm.stream`: `voice.llm.ttft_ms` (time-to-first-token), `voice.llm.total_tokens_out`, `voice.llm.provider` (= `groq` here), `voice.llm.model` (= `llama-3.3-70b-versatile`)
- `voice.tts.stream`: `voice.tts.first_chunk_ms` (time to first audio chunk), `voice.tts.total_chars`, `voice.tts.provider` (= `deepgram`), `voice.tts.voice_id`

---

## Provider configuration

All API keys are already Fly secrets on `orchet-voice` from Phase 1 setup. Don't re-provision:

| Provider | Fly secret | Use |
|---|---|---|
| Deepgram | `LUMO_DEEPGRAM_API_KEY` | STT + TTS (both Nova-3 + Aura-2 share the same key) |
| Groq | `GROQ_API_KEY` | LLM (default fast path per ADR) |
| Anthropic | `ANTHROPIC_API_KEY` | Not used in Phase 2 (Phase 3 enables Claude path for quality agents) |

Pipecat services to use (from `pipecat-ai` 0.0.61):

- `pipecat.services.deepgram` → `DeepgramSTTService` and `DeepgramTTSService`
- `pipecat.services.groq` → `GroqLLMService`

If any of those module paths changed in 0.0.61, use the available equivalent and note it in the PR description. Do NOT upgrade Pipecat.

LLM settings:
- Model: `llama-3.3-70b-versatile` (Groq's flagship; ~150ms TTFT)
- System prompt: minimal voice-friendly default — see § Voice system prompt below
- Streaming: true
- Max tokens: 250 (voice answers should be short)
- Temperature: 0.7
- Tool calling: **disabled** (no function calls in Phase 2)

STT settings:
- Model: `nova-3` (Deepgram's latest)
- Streaming: true
- Interim results: true
- Endpointing: 300ms (Deepgram's smart endpointing)
- Languages: `en-US` only (Sarvam adds Indian languages in Phase 4)

TTS settings:
- Model: `aura-2-andromeda-en` (default neutral voice — pick a different `aura-2-*` voice if you have a strong preference; document the choice)
- Format: `mp3` (compatible with Daily transport's audio pipeline)
- Sample rate: 24000
- Streaming: true
- Sentence-level chunking: true (first audio chunk emits on first complete sentence from LLM)

---

## Voice system prompt (default for Phase 2)

Voice agents need shorter, more conversational prompts than text. Use this default in `voice/prompts/default_voice.txt`:

```
You are a helpful voice assistant for Orchet.

Constraints for voice mode:
- Speak conversationally, in 1–3 short sentences.
- No markdown, no bullet points, no code blocks.
- If the user asks something requiring up-to-the-minute data you don't have, say so and ask if they'd like you to search.
- If the answer is genuinely uncertain, say "I'm not sure" rather than guess.
- Don't repeat the user's question back to them.
- Use natural pauses; don't list more than three options at once.

If the user says "stop" or "wait", acknowledge and pause briefly.
```

Phase 3 will add per-agent prompt overrides; Phase 2 just uses this default.

---

## Client-side VAD (Silero WASM)

Update the Phase 1 web smoke-test page (`tests/smoke/web-client.html`) to add Silero VAD:

- Load Silero VAD via the `@ricky0123/vad-web` npm package (or equivalent WASM build)
- Initialize with default thresholds
- When VAD reports "speech started" mid-agent-response, emit a `barge_in` event over the WebRTC data channel
- When VAD reports "speech ended", flush the audio buffer

Pipecat side: subscribe to the `barge_in` event from the data channel. On receipt: cancel the TTS stream, flush the audio output, restart the STT listener. Use Pipecat's `InterruptionProcessor` or equivalent for 0.0.61.

iOS Silero VAD is Phase 6 — not in this brief.

---

## Async transcript persistence (off the hot path)

Every assistant turn fires a background HTTP POST to:

```
POST https://api.orchet.ai/sessions/{session_id}/messages
Authorization: Bearer <ORCHET_INTERNAL_TOKEN>
Content-Type: application/json
{
  "session_id": "voice_...",
  "turn_id": "...",
  "user_id": "<from JWT>",
  "channel": "voice",
  "messages": [
    {"role": "user", "content": "<final transcript>"},
    {"role": "assistant", "content": "<full LLM response>"}
  ],
  "latency_ms": {
    "stt_first_partial": ...,
    "llm_ttft": ...,
    "tts_first_chunk": ...,
    "total_mouth_to_ear": ...
  }
}
```

If the persistence call fails, do NOT block the voice pipeline. Log to Honeycomb as `voice.persistence.failed` and continue. Phase 3 adds full session reconstruction; for now persistence is fire-and-forget.

The route `/sessions/{id}/messages` already exists on `orchet-backend` — verify by curl before relying on it; if it doesn't, use a placeholder that just logs the payload locally and surface this as a Phase 3 follow-up.

---

## Stop conditions (must report, not work around)

- **Pipecat 0.0.61 doesn't have `DeepgramSTTService`** — pin a compatible version OR vendor the service class with attribution; report which
- **Groq Llama 3.3 70B 422s on the `llama-3.3-70b-versatile` model name** — Groq sometimes renames; check their docs, use the closest equivalent, note in PR
- **Daily transport drops audio chunks under barge-in** — known regression on some versions; pin compatible version, report
- **Latency > 1.5s p50 for US East voice turns** — that's worse than acceptable. Don't ship; debug. Common offenders: not actually streaming, double-buffering on TTS, blocking persistence call
- **Aura-2 first-chunk latency > 300ms** — try a different voice (some Aura-2 voices have warmup overhead); report
- **`/sessions/{id}/messages` route doesn't exist on backend** — defer persistence to Phase 3; note in PR

---

## Verification checklist

Before opening the PR:

- [ ] `uv sync --frozen` passes
- [ ] `uv run ruff check .` + `uv run ruff format --check .` pass
- [ ] `uv run pyright voice/ tests/` passes
- [ ] `uv run pytest -v` passes — include new tests:
  - `test_pipeline_stt_streaming.py` (mocks Deepgram, asserts span emission)
  - `test_pipeline_llm_streaming.py` (mocks Groq, asserts TTFT span attribute)
  - `test_pipeline_tts_streaming.py` (mocks Aura-2, asserts first-chunk span attribute)
  - `test_barge_in.py` (asserts TTS cancels on barge-in event)
- [ ] Docker image builds
- [ ] `fly deploy --strategy rolling` to `iad` succeeds (uses existing app from Phase 1)
- [ ] Smoke test from web client:
  - "What time is it in Tokyo?" returns a spoken English answer
  - Mouth-to-ear p50 < 1.0s for US East
  - Interruption test: speaking over the agent stops TTS within 300ms
- [ ] Honeycomb shows `voice.stt.stream`, `voice.llm.stream`, `voice.tts.stream` spans within 1 minute of smoke test
- [ ] No secrets in diff
- [ ] No real user audio committed
- [ ] No `voice/turn` or tool-execution paths added

---

## PR structure

Single PR to `Orchet-AI/orchet-voice`:

**Title:** `VOICE-PHASE-2: streaming STT/LLM/TTS pipeline + barge-in`

**Body:**
- Pipeline composition (Pipecat services used + versions)
- Phase 1 echo → Phase 2 streaming diff summary
- Smoke test result: mouth-to-ear p50/p95 for US East
- Interruption latency (barge-in → TTS cancelled) measurement
- Honeycomb screenshot or query link showing the new spans
- Any deviations from this brief + reasoning
- Phase 3 readiness checklist (which Phase 3 prep is done, what's left)

Tag @Prasanth-Kalas as reviewer. Update tracking issue in orchet-voice with status as you progress.

---

## What "done" looks like

Phase 2 is complete when:

1. PR merged to `orchet-voice` main
2. `wss://orchet-voice.fly.dev` accepts a voice turn and responds verbally
3. Interruption (barge-in) works and is documented with a measurement
4. `voice.stt.stream`, `voice.llm.stream`, `voice.tts.stream` spans visible in Honeycomb
5. Mouth-to-ear p50 < 1.0s for US East, p95 < 1.4s (achievable per ADR; flag if you can't hit)
6. Smoke-test page in repo at `tests/smoke/web-client.html` works for a person clicking through it
7. Status report posted on tracking issue summarizing p50/p95 + barge-in latency + LLM provider in use

After Phase 2 closes, Phase 3 (orchestrator integration + visual confirmation for high-risk actions) is the next dispatchable lane.

---

## References

- [VOICE-ARCHITECTURE-1 ADR v6](../architecture/VOICE-ARCHITECTURE-1.md)
- [VOICE-PHASE-1-CODEX-BRIEF.md](./VOICE-PHASE-1-CODEX-BRIEF.md) — predecessor; must be merged first
- [voice-turn-contract-proposal.md](../voice-turn-contract-proposal.md) — Phase 3's contract; mentioned here so you understand why Phase 2 doesn't include tool execution
- [Pipecat 0.0.61](https://github.com/pipecat-ai/pipecat/releases/tag/v0.0.61)
- [Deepgram Nova-3 streaming docs](https://developers.deepgram.com/docs/measuring-streaming-latency)
- [Deepgram Aura-2 streaming docs](https://developers.deepgram.com/docs/tts-websocket)
- [Groq Llama 3.3 70B](https://console.groq.com/docs/models#llama-3-3-70b-versatile)
- [Silero VAD](https://github.com/snakers4/silero-vad)
