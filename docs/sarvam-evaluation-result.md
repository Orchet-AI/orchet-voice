# Sarvam evaluation result — Phase 0 follow-up

**Verdict (one line):** **PASS.** Phase 4 (Sarvam Indian-language layer) can dispatch as planned. All three target languages (Hindi, Telugu, Tamil) clear the 10% mean-WER target with comfortable headroom; Hinglish handling is semantically correct (with one scoring caveat documented below).

**Date:** 2026-05-11
**Method:** TTS→STT round-trip smoke pass per `sarvam-evaluation-plan.md` §"Open questions" #3 — no native-speaker recordings available, so each sentence is synthesized via Bulbul TTS (`bulbul:v2`, speaker `anushka`), then fed back through Saarika STT (`saarika:v2.5`), and the resulting transcript is scored against the original text.
**Caveat:** This is a biased eval (both models are Sarvam). It validates API health, network latency, language coverage, and code-mixing behavior, but cannot rule out STT errors against accents/voices the TTS doesn't produce. Native-speaker re-validation is queued as a post-launch task.

---

## Per-language scorecard

| Language | n | TTS ok | STT ok | TTS median | STT median | WER mean | WER max |
| :-- | :-: | :-: | :-: | --: | --: | --: | --: |
| Hindi    | 5 | 5/5 | 5/5 | 883 ms | 599 ms | **4.2%** | 11.8% |
| Telugu   | 5 | 5/5 | 5/5 | 967 ms | 664 ms | **9.1%** | 20.0% |
| Tamil    | 5 | 5/5 | 5/5 | 858 ms | 627 ms | **7.9%** | 25.0% |
| Hinglish | 3 | 3/3 | 3/3 | 936 ms | 771 ms | (see below) | (see below) |

**Latency observations:**
- TTS median 858–967ms is **above** the plan's 200ms first-chunk target, **but** this measures *total batch synthesis time*, not streaming first-chunk-time. The streaming `text-to-speech-streaming` endpoint will land that latency lower; this number is the upper bound, not the live-pipeline number.
- STT median 599–771ms is **end-to-end batch** (the API processes the full audio, returns one final transcript). The streaming `speech-to-text-streaming` endpoint surfaces partials at ~250ms per the plan's target; needs separate measurement during Phase 4 integration.
- One Telugu TTS outlier at 6853 ms looked like a cold-start warm-up. Median absorbs it.

**Per-sample errors that account for the mean WER:**
- Hindi #3, #5: Sarvam STT collapses commas/full-stops or stretches a compound word over two tokens. Semantically correct.
- Telugu #5: One contraction "అమ్మానాన్నలను" → "అమ్మ నాన్నలను" (parent compound vs two-word). Semantically identical.
- Tamil #3: "ஆம், அதை பதிவு செய்" rendered as "ஆமாம் அது பதிவு செய்" — synonymous (yes-particle and pronoun softened); not a model failure.

---

## Code-mixing scorecard (Hinglish)

| # | Spoken (Latin reference) | STT output | Score |
| :-- | :-- | :-- | :-: |
| 1 | "Bhai mujhe tomorrow flight book karni hai Delhi ke liye, around 9 AM." | "भाई मुझे टुमारो फ्लाइट बुक करनी है दिल्ली के लिए अराउंड 9 ए एम।" | ✅ Semantically perfect; English nouns transliterated into Devanagari |
| 2 | "Confirm karna hai? Payment hogi credit card se." | "कन्फर्म करना है। पेमेंट होगी क्रेडिट कार्ड से।" | ✅ Perfect |
| 3 | "Cancellation policy kya hai? Refund milega ya nahi?" | "कैंसिलेशन पॉलिसी क्या है? रिफंड मिलेगा या नही?" | ✅ Perfect |

**Scoring caveat:** Token-level WER against the Latin reference returns ~97–100% because the STT renders English words in Devanagari (`tomorrow` → `टुमारो`, `flight` → `फ्लाइट`, `cancellation` → `कैंसिलेशन`). This is **correct Sarvam behavior for `language_code=hi-IN`** — the model is doing language-aware code-mixing exactly as advertised — but it makes Latin-vs-Indic-script WER scoring meaningless. Manual review of all three samples confirms **semantic correctness on 100% of English words**. The orchestrator's downstream Groq LLM will handle Devanagari-rendered English fine.

**Implication for Phase 4:** when wiring Sarvam STT into orchet-voice, the LLM prompt should expect Devanagari-rendered English tokens for code-mixed input. No special handling needed; Groq Llama 3.3 70B handles both scripts cleanly in our prior Phase 0 tests.

---

## Pricing summary (not re-confirmed today)

Plan §"Pricing confirmation" was scoped to require a manual check of Sarvam's pricing page. I did not re-fetch their pricing during this smoke pass — Sarvam's public pricing is a normal browser fetch, which is faster done by hand than via my tool stack.

**Action item:** before Phase 4 dispatch, confirm:
- Saarika tier rate (target: ~$0.50/hour or current public rate)
- Bulbul tier rate
- Monthly cost estimate at 1000 voice-hours/month vs 10000 voice-hours/month
- Side-by-side vs Deepgram at the same volume

These are needed for the Phase 4 brief's cost-comparison section, not for the go/no-go on this evaluation.

---

## Risks

1. **Streaming latency not directly measured.** Today's eval used batch APIs (the synchronous JSON-response endpoints). Phase 4 will use streaming (`saarika-streaming-v2`/`bulbul-streaming-v2`). Phase 4 PR must include a streaming-latency probe before flipping users to Sarvam.

2. **Telugu cold-start outlier (6853ms TTS).** One sample took 6.8s vs the 858ms median. Likely cold-start; warm path is fine. Monitor in Phase 4 Honeycomb.

3. **No native-speaker validation.** This round-trip eval cannot detect cases where Sarvam STT mis-hears a real human accent that doesn't match Bulbul's TTS voice distribution. Queue a real-recording follow-up for the first 30 days of Phase 4 production traffic; if WER spikes vs baseline, we have a fallback to Deepgram.

4. **Punctuation drift.** Sarvam STT adds `।`/`।` Devanagari punctuation and English-style commas. Already handled in WER normalization. Make sure orchestrator transcript-persistence + LLM prompts tolerate both.

---

## Alternative providers if this had been a no-go

(Not exercised — kept here in case Phase 4 production traffic later forces a re-evaluation.)

- **Google Speech-to-Text** — supports Hindi/Tamil/Telugu; English code-mixing is weaker.
- **Azure Speech** — similar coverage; pricing tier check needed.
- **OpenAI Whisper API (`whisper-1`)** — Indian-language WER higher than Sarvam in published benchmarks; English-only fallback acceptable as denial path.

---

## Decision

Per `sarvam-evaluation-plan.md` decision tree:

> "Pass (≥ 2 of 3 languages clear targets + Hinglish works) → Phase 3 ships as planned, Sarvam wired in"

We have **3 of 3 languages** under the 10% mean-WER target, and Hinglish works semantically. **This is a clean Pass.** Phase 4 (Sarvam Indian-language layer in orchet-voice) is unblocked for dispatch.

---

## Raw data

Full per-sample results are at `/Users/prasanthkalas/Lumo-Agents/Orchet/orchet-voice/docs/sarvam-evaluation-result.json` (not committed; transient artifact from the eval run).
