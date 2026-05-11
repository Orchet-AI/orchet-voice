# Codex brief — VOICE-PHASE-5: Multi-region + per-agent LLM router

**Brief ID:** VOICE-PHASE-5-CODEX
**Parent ADR:** [VOICE-ARCHITECTURE-1 v6](../architecture/VOICE-ARCHITECTURE-1.md)
**Predecessors:** Phase 1–4 all merged. Phase 1b India RTT result must guide whether `bom` is promoted or stays Phase 5.
**Status:** Drafted — dispatches after Phase 4 merges
**Owner:** Codex
**Reviewer:** Kalas + Claude
**Estimated effort:** 5 days

This brief is lighter than Phase 2/3. Region selection depends on Phase 1b actual measurements + actual user geography distribution at the time. Update before dispatch.

---

## Goal

Deploy `orchet-voice` to multiple Fly regions for sub-900ms p50 globally. Add per-agent LLM router so Claude-quality agents (booking, quote-aware) pay for Claude Sonnet while quick-chat agents stay on Groq.

---

## Predecessor gates

- Phase 4 (Sarvam) merged
- Phase 1b India RTT result reviewed; final region choice for APAC confirmed
- Per-agent LLM preference declared in the agent manifest (Phase 3 added the schema field; this brief adds the routing logic)
- Daily Cloud account ready for higher voice-minute volume (might need plan upgrade; verify before dispatch)

---

## Hard scope boundaries

**You MUST NOT:**
- Add any new providers (Phase 4 finalized the provider set)
- Change Phase 3 safety boundaries
- Touch iOS (Phase 6)

**You MUST:**
- Deploy `orchet-voice` to **4 regions**: `iad` (US East — existing), `fra` (Frankfurt — EU), `sin` (Singapore — APAC), `bom` (Mumbai — APAC; promote from probe to production based on Phase 1b)
- Configure `min_machines_running=1` per region
- Add per-agent LLM router: read `agent_manifest.llm_preference` ∈ {`groq`, `anthropic`, `openai`} and dispatch accordingly
- Add cost telemetry: per-voice-minute breakdown by `voice.llm.provider` × `voice.locale`
- Region routing: Daily Cloud handles the SFU side automatically; orchet-voice just needs Machines in each region

---

## Region commitment + cost

| Region | Slug | Cost/mo | Justification |
|---|---|---|---|
| US East | `iad` | $32 | Existing from Phase 1 |
| Frankfurt | `fra` | $32 | EU users; complies with data residency soft preference |
| Singapore | `sin` | $40 | APAC primary |
| Mumbai | `bom` | $40 | India users — promoted from probe based on Phase 1b RTT result |
| **Total** | | **~$144/mo** | per ADR v6 cost model |

Daily Cloud routes SFU traffic to nearest user region — no extra config needed once Fly Machines exist.

---

## Per-agent LLM router

Agent manifest extension (already added in Phase 3 by the tool registry work):

```yaml
agents:
  - agent_id: lumo-rentals-trip-planner
    llm_preference: anthropic        # quote-aware, prefer quality
    llm_model: claude-sonnet-4-6
  - agent_id: lumo-rentals-chat
    llm_preference: groq             # quick chat, prefer speed
    llm_model: llama-3.3-70b-versatile
  - agent_id: customer-support
    llm_preference: anthropic
    llm_model: claude-sonnet-4-6
```

Phase 5 implements the router that reads this and instantiates the right Pipecat LLM service per session.

---

## Deliverable: single PR to `orchet-voice`

**Title:** `VOICE-PHASE-5: multi-region deploy + per-agent LLM router + cost telemetry`

Scope:
- `fly.toml` updated with the 4 regions
- `fly deploy` runs against all 4 regions
- `voice/routing/llm_router.py` — reads agent manifest, picks LLM
- `voice/obs/cost.py` — per-session cost telemetry (voice-minute counter × provider rate)
- Honeycomb panel addition: "Cost per voice-minute by agent"
- Smoke tests from real clients in 4 regions (Kalas + helpers; or synthetic via Fly's region-pinning)

---

## Stop conditions

- **Fly Machine in a region won't boot** — investigate; might be a regional capacity issue at Fly; report
- **Daily Cloud routes to wrong region under load** — investigate Daily's region-pinning settings; report
- **Per-agent LLM router routes wrong model under high concurrency** — fix race condition; this is a Pipecat session lifecycle question
- **Cost telemetry shows agent-burn unexpectedly high** — pause; might be a runaway loop in the pipeline

---

## Verification checklist

- [ ] Fly Machines running in all 4 regions
- [ ] Mouth-to-ear p50 < 900ms for at least 3 of 4 regions (US, EU, APAC primary)
- [ ] Per-agent LLM router routes correctly under load (10+ concurrent sessions)
- [ ] Cost telemetry visible in Honeycomb
- [ ] No regression in Phase 1–4 behavior
- [ ] Daily Cloud cost dashboard checked + within budget

---

## What "done" looks like

1. PR merged
2. Voice latency p50 < 900ms in all 4 production regions
3. Cost dashboard populated for at least 24h of representative traffic
4. Per-agent LLM split verifiable in Honeycomb (Groq sessions vs Anthropic sessions)
5. Status report posted with per-region p50 + cost-per-conversation

After Phase 5 merges, voice is production-ready globally. Phase 6 (iOS WebRTC client + voice eval framework + production hardening) becomes the ongoing post-launch lane.

---

## References

- [Fly regions list](https://fly.io/docs/reference/regions/)
- [Daily Cloud regional SFU](https://docs.daily.co/reference/daily-js)
