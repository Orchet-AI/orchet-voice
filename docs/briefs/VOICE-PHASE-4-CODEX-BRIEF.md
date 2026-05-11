# Codex brief — VOICE-PHASE-4: Sarvam Indian-language layer

**Brief ID:** VOICE-PHASE-4-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Sarvam plan:** [sarvam-evaluation-plan.md](../sarvam-evaluation-plan.md)
**Predecessors:** Phase 1 + Phase 2 + Phase 3 merged. Sarvam evaluation result (`docs/sarvam-evaluation-result.md`) must exist with a Pass / Partial-pass / Fail verdict.
**Status:** Drafted — dispatches after Phase 3 merges AND Sarvam evaluation passes
**Owner:** Codex
**Reviewer:** Kalas + Claude
**Estimated effort:** 4 days

This brief is intentionally lighter than Phase 2/3. Some details depend on Phase 1b India RTT outcome and the Sarvam evaluation result. Fill those in before dispatch.

---

## Goal

Add Sarvam (STT + TTS) as a parallel provider to Deepgram, with a language-detection routing layer in front. English/EU traffic stays on Deepgram; detected Indian languages route to Sarvam Saarika STT + Bulbul TTS. Hinglish (code-mixed) traffic uses Sarvam end-to-end because Deepgram doesn't handle it.

---

## Predecessor gates

Do NOT start this brief until:

1. **Phase 3 PRs merged** — voice safety boundary must be in place; Sarvam-mediated voice can also book flights, the safety policy must apply
2. **`docs/sarvam-evaluation-result.md` exists and shows Pass or Partial-pass** (per `sarvam-evaluation-plan.md` decision tree)
3. **Sarvam account provisioned** under Orchet-AI, API key added as Fly secret `SARVAM_API_KEY` on `orchet-voice`

If Sarvam evaluation result is Fail or missing: STOP. Re-evaluate provider (Google STT, Azure Speech, OpenAI Whisper API for Indian languages) and write a different brief.

---

## Hard scope boundaries

**You MUST NOT:**
- Replace Deepgram for English/EU — both providers coexist
- Add multi-region (Phase 5)
- Change Phase 3 safety boundaries (`/voice/turn` policy still applies)

**You MUST:**
- Add language detection on the STT input (Pipecat's language-detection processor OR a small pre-classifier)
- Route detected `hi` (Hindi), `te` (Telugu), `ta` (Tamil), and Hinglish to Sarvam Saarika STT
- Route detected `en-*` to Deepgram Nova-3
- Match TTS provider to STT language (Bulbul for Indian, Aura-2 for English)
- Handle code-mixing within a single turn (Hinglish — most common case for India users)
- Emit new spans: `voice.lang.detect`, `voice.stt.sarvam`, `voice.tts.sarvam` with attribute `voice.locale` on every span
- Default voice system prompt is per-language (English from Phase 2 stays; add Hindi/Telugu/Tamil translations)

---

## Deliverable: single PR to `orchet-voice`

**Title:** `VOICE-PHASE-4: Sarvam Indian-language layer + language routing`

Scope per repo-scaffold-plan.md § Phase 4. Notable files:

- `voice/providers/stt_sarvam.py` (new)
- `voice/providers/tts_sarvam.py` (new)
- `voice/routing/language_router.py` (new — picks STT provider per language)
- `voice/prompts/default_voice_hi.txt`, `default_voice_te.txt`, `default_voice_ta.txt` (new — Indian-language system prompts)
- Updated tests for the routing logic

---

## Stop conditions

- **Sarvam Saarika streaming WebSocket has different shape than Deepgram** — wrap in a common interface, document
- **Pipecat 0.0.61 has no language-detection processor** — vendor a lightweight one (Whisper-tiny based, run on first 2-3s of audio) and report
- **Hinglish detection accuracy < 80%** — fall back to default route (English → Deepgram); document
- **Sarvam pricing exceeds the evaluation estimate by > 2x** — pause, report; we'd want to renegotiate before scaling

---

## Verification checklist

- [ ] `uv run pytest` includes new language-routing tests
- [ ] Smoke test: Telugu speaker says a sentence, gets a Telugu voice response
- [ ] Smoke test: Hinglish speaker (`bhai book karni hai flight to Delhi`) gets a code-mixed response handled correctly
- [ ] Smoke test: English speaker still goes through Deepgram (no regression)
- [ ] `voice.lang.detect`, `voice.stt.sarvam`, `voice.tts.sarvam` spans visible in Honeycomb
- [ ] No regression in English mouth-to-ear p50 (must stay < 1.0s)

---

## What "done" looks like

1. PR merged to `orchet-voice` main
2. Three language smoke tests pass (Telugu, Hinglish, English unchanged)
3. Phase 0 Honeycomb dashboard's per-stage panel now shows STT split between `deepgram` and `sarvam` providers
4. Status report on tracking issue with per-language p50 measurements

After Phase 4 merges, Phase 5 (multi-region) is the next dispatchable lane.

---

## References

- [Sarvam evaluation plan](../sarvam-evaluation-plan.md)
- [Sarvam AI docs](https://docs.sarvam.ai/)
- [Pipecat language-aware pipelines](https://docs.pipecat.ai/)
