# Phase 0 — Sarvam evaluation plan

**Status:** Approved, ready to execute
**Owner:** TBD (Codex / contractor / Kalas)
**Duration:** ~1 day (runs in parallel with the measurement plan)
**Goal:** De-risk the Phase 3 Sarvam integration by validating quality, latency, and pricing on real Indian-language voice samples. Phase 3 only ships if this evaluation passes.

---

## Why we de-risk now

Phase 3 budgets 4 days for the Sarvam Indian-language layer. That budget assumes:

1. Sarvam Saarika STT is accurate enough on the languages we care about
2. Sarvam Bulbul TTS sounds natural enough for a conversational agent
3. Code-mixing (Hinglish — Hindi + English in the same sentence) works
4. Streaming latency is within our budget (target: ≤ 250 ms first STT partial, ≤ 200 ms first TTS chunk)
5. Pricing is what their public page says it is

If any of these fail at evaluation time, Phase 3 either changes provider (back to Deepgram with worse Indian-language quality) or slips by weeks while we evaluate alternatives. Catching this now is cheap; catching it during Phase 3 is expensive.

---

## Smoke test corpus

### Languages

Three primary languages for first evaluation, in order of expected user volume:

| Priority | Language | Script | Why |
|---|---|---|---|
| 1 | Hindi | Devanagari | Largest population; default fallback when language detection is unsure |
| 2 | Telugu | Telugu | Kalas's home language; high personal validation signal |
| 3 | Tamil | Tamil | Large South India user base; phonetically distinct from Hindi/Telugu |

Add Kannada and Bengali in a follow-up evaluation if these three pass.

### Test sentences (5 per language, plus 3 code-mixed)

Each sentence is chosen to exercise different voice-agent intents:

| # | Intent | English equivalent |
|---|---|---|
| 1 | Search | "Find me a flight from Hyderabad to Delhi tomorrow morning" |
| 2 | Compare | "Which is cheaper, the train or the bus to Bangalore?" |
| 3 | Confirm | "Yes, book it. Use my saved card." |
| 4 | Refuse | "No, I don't want vegetarian. Show me other options." |
| 5 | Open-ended | "I want to visit my parents next weekend and I'm not sure how to get there." |

Translate these five into Hindi, Telugu, Tamil. Record 5 native-speaker samples per language (15 audio files total). Native-speaker recordings matter — accent + prosody affect STT accuracy more than vocabulary choice.

### Code-mixing tests (Hinglish)

Three sentences that mix English nouns/verbs into a Hindi grammatical frame — extremely common in real Indian conversational voice:

1. *"Bhai mujhe tomorrow flight book karni hai Delhi ke liye, around 9 AM."*
2. *"Confirm karna hai? Payment hogi credit card se."*
3. *"Cancellation policy kya hai? Refund milega ya nahi?"*

Most STT providers (including Deepgram) handle these poorly. Sarvam claims native code-mixing support; this test confirms.

---

## Metrics to collect

For each sample (15 monolingual + 3 Hinglish = 18 audio files):

### STT (Saarika streaming)

| Metric | Target | How measured |
|---|---|---|
| First-partial latency | ≤ 250 ms | Timestamp from "audio start" to first non-empty partial |
| Final-transcript latency | ≤ 500 ms after endpointing | Timestamp from "audio end" to final transcript |
| Word error rate (WER) | ≤ 10% per language | Compare transcript to native-speaker ground truth |
| Code-mixing accuracy | English nouns preserved verbatim | Manual review of 3 Hinglish samples |

### TTS (Bulbul streaming)

| Metric | Target | How measured |
|---|---|---|
| First-chunk latency | ≤ 200 ms | Timestamp from "synthesize call" to first audio byte |
| Voice quality | Subjective ≥ 4/5 | Three reviewers rate on naturalness, prosody, emotion |
| Voice diversity | ≥ 2 voices per language | Confirm Sarvam offers male + female voices we can pick from |

### Pricing confirmation

- Confirm Saarika tier pricing matches public docs ($0.50/hour or current)
- Confirm Bulbul tier pricing matches public docs ($X/M chars)
- Estimate monthly cost at 1000 voice-hours/month (100% Indian users), 10000 voice-hours/month
- Compare side-by-side against Deepgram at same volume

---

## Tools used for the evaluation

- A simple Python script that:
  - Reads each audio file
  - Streams it to Saarika STT WebSocket
  - Logs first-partial + final-transcript timestamps
  - Compares transcript to ground truth (computes WER)
  - For TTS: sends the English ground truth back through Bulbul, measures first-chunk latency
- Native speaker reviewers (Kalas for Telugu; recruit two friends for Hindi and Tamil if needed)
- Side-by-side WER comparison with Deepgram on the same audio (Deepgram doesn't support all three languages, but worth a comparison where it does)

---

## What "done" looks like

A 2-page markdown report (`docs/sarvam-evaluation-result.md`) checked into this repo with:

1. **Go/no-go recommendation** — single sentence at the top
2. **Per-language scorecard** — WER, latency, quality rating for Hindi, Telugu, Tamil
3. **Code-mixing scorecard** — accuracy + qualitative notes
4. **Pricing summary** — confirmed rates, monthly cost estimate at our expected volumes
5. **Risks** — anything that suggests we should hedge Phase 3 plan
6. **Alternative providers if no-go** — Google STT, Azure Speech, OpenAI Whisper API for Indian languages

---

## Decision tree for Phase 3 readiness

- **Pass** (≥ 2 of 3 languages clear targets + Hinglish works) → Phase 3 ships as planned, Sarvam wired in
- **Partial pass** (1 of 3 languages fails) → Phase 3 ships for the passing languages, document fallback to Deepgram for the failing language
- **Fail** (≥ 2 languages fail) → Stop. Re-evaluate provider; consider Google STT / Azure / Whisper. Phase 3 slips by 1–2 weeks.

---

## Open questions

1. **Streaming or batch first?** Sarvam supports both; streaming is what we need for Phase 3, but batch is easier to evaluate quickly. Default: batch for quality scoring, streaming for latency-only.
2. **Test on web Daily transport or directly via Sarvam SDK?** Directly via Sarvam SDK for the smoke test (isolates Sarvam from Daily transport overhead).
3. **Native-speaker recording cost?** If we can't recruit native speakers for Tamil and Hindi, fall back to TTS-generated samples from Sarvam's own voices for input — this biases the test but is acceptable for a smoke pass.

---

## Phase 3 readiness gate

This evaluation result + the Phase 0 measurement baseline together form the Phase 1 readiness gate. Phase 1 can start once Phase 0 measurement is live, even if Sarvam evaluation is still in progress — Sarvam doesn't gate Phases 1 or 2.
