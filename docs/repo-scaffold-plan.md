# Repo scaffold plan

**Status:** Approved — describes what Phase 1 will build
**Owner:** Voice service author
**Scope:** Defines the orchet-voice repo layout, dependencies, env manifest, and CI. Phase 0 commits docs only. Phase 1 implements this scaffold.

This document is the implementation blueprint for the orchet-voice repo's Phase 1 skeleton. Phase 0 does NOT write any of this code — it only commits this plan.

---

## Target stack

| Layer | Choice | Reason |
|---|---|---|
| Language | **Python 3.12** | Pipecat is Python-native; FastAPI is the standard for service control planes |
| Web framework | **FastAPI** | Control plane (health, debug endpoints, signaling assist), not the audio pipeline |
| Voice pipeline | **Pipecat OSS** (pin pre-0.0.62 if Daily transport audio regression unresolved) | Pipeline-first framework; audio handling is hard, reuse |
| WebRTC transport | **Daily Cloud** | By Pipecat creators, $0.01/min, handles SFU + global region routing |
| STT default | **Deepgram Nova-3** (streaming) | Already in our infra, English/EU quality |
| STT Indian | **Sarvam Saarika** (Phase 4) | Day 1 is Deepgram only; Sarvam wires later |
| LLM default | **Groq Llama 3.3 70B** | ~150 ms TTFT; cheap |
| LLM quality | **Anthropic Claude Sonnet** | Per-agent override; ~500 ms TTFT |
| TTS default | **Deepgram Aura-2** (streaming) | Already integrated; ~100 ms first chunk |
| TTS Indian | **Sarvam Bulbul** (Phase 4) | Day 1 is Aura-2 only |
| Host | **Fly.io Machines** | Long-lived persistent service; multi-region |
| Observability | **OpenTelemetry → Honeycomb** | Matches existing backend pattern |
| Container | **Docker + uv** | Reproducible deps via uv; fast cold builds on Fly |
| CI | **GitHub Actions** | Same pattern as other Orchet-AI repos |

---

## Directory layout

```
orchet-voice/
├── README.md                       # 1-page overview, links to ADR
├── docs/
│   ├── architecture/
│   │   └── VOICE-ARCHITECTURE-1.md # the approved ADR
│   ├── phase-0-measurement-plan.md
│   ├── sarvam-evaluation-plan.md
│   ├── voice-turn-contract-proposal.md
│   └── repo-scaffold-plan.md       # this doc
├── voice/
│   ├── __init__.py
│   ├── server.py                   # FastAPI app entrypoint
│   ├── pipeline.py                 # Pipecat pipeline construction
│   ├── transport.py                # Daily WebRTC adapter wiring
│   ├── auth.py                     # Bearer JWT validation
│   ├── routes/
│   │   ├── health.py               # GET /health
│   │   └── debug.py                # POST /debug/echo (dev only)
│   ├── providers/
│   │   ├── stt_deepgram.py
│   │   ├── llm_groq.py
│   │   ├── llm_anthropic.py
│   │   └── tts_deepgram.py
│   └── obs/
│       ├── tracing.py              # OTel setup
│       └── logging.py              # structured logging
├── tests/
│   ├── conftest.py
│   ├── test_health.py
│   ├── test_auth.py
│   └── test_pipeline_echo.py       # in-memory pipeline smoke
├── Dockerfile
├── fly.toml                        # Fly app config
├── pyproject.toml                  # uv-managed deps
├── uv.lock
├── .env.example
├── .github/
│   └── workflows/
│       ├── ci.yml                  # lint + typecheck + test
│       └── deploy.yml              # build + deploy to Fly on main
└── .gitignore                      # already set by repo create
```

---

## Env variable manifest

All env vars are set via Fly secrets (`fly secrets set KEY=value`). NEVER commit secrets to the repo. `.env.example` documents the names with placeholder values.

| Env | Required | Description | Source |
|---|---|---|---|
| `ORCHET_VOICE_ENV` | yes | `production` / `staging` / `dev` | Set per Fly app |
| `ORCHET_INTERNAL_TOKEN` | yes | Service-to-service token for backend calls | Same value as `orchet-backend` |
| `ORCHET_GATEWAY_URL` | yes | `https://api.orchet.ai` | Static |
| `ORCHET_HONEYCOMB_API_KEY` | yes | OTel exporter auth | Existing Honeycomb account |
| `NEXT_PUBLIC_SUPABASE_URL` | yes | For JWT validation on connection-open | Same as backend |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | yes | For JWT validation | Same as backend |
| `DEEPGRAM_API_KEY` | yes | STT + TTS | Existing in `lumo-ml-service` Modal Secret; rotate or share |
| `GROQ_API_KEY` | yes | Default LLM | New — provision in Phase 1 |
| `ANTHROPIC_API_KEY` | yes | Quality LLM | Existing in `orchet-backend` Render env; share |
| `DAILY_API_KEY` | yes | WebRTC transport | New — provision in Phase 1 (Daily Cloud account) |
| `DAILY_ROOM_DOMAIN` | yes | e.g. `orchet.daily.co` | New from Daily account setup |
| `SARVAM_API_KEY` | no (Phase 4) | Indian-language STT/TTS | New — provision after Sarvam evaluation passes |
| `ORCHET_VOICE_REGION` | yes | `iad` / `bom` / `sin` / `fra` | Set per Fly Machine instance |
| `ORCHET_VOICE_LLM_DEFAULT` | yes | `groq` / `anthropic` / `openai` | Defaults to `groq` |
| `ORCHET_VOICE_MIN_TRANSCRIPT_FOR_LLM` | no | partial-trigger threshold | Defaults to 3 words |

---

## Dependencies (initial)

`pyproject.toml` Phase 1 dependencies:

```toml
[project]
name = "orchet-voice"
version = "0.1.0"
requires-python = ">=3.12"

dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "pipecat-ai==0.0.61",        # pinned <0.0.62 per ADR risk note
  "pipecat-ai[daily]==0.0.61",
  "pipecat-ai[deepgram]==0.0.61",
  "pipecat-ai[groq]==0.0.61",
  "pipecat-ai[anthropic]==0.0.61",
  "supabase>=2.9",             # JWT validation
  "opentelemetry-api>=1.27",
  "opentelemetry-sdk>=1.27",
  "opentelemetry-instrumentation-fastapi>=0.48",
  "opentelemetry-exporter-otlp-proto-http>=1.27",
  "structlog>=24.4",
  "httpx>=0.28",               # for /voice/turn round-trips to backend
  "pydantic>=2.9",
]

[dependency-groups]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.7",
  "pyright>=1.1",
]
```

Locked via `uv.lock`. Container build uses `uv sync --frozen --no-dev` for reproducibility.

---

## Health endpoint

`GET /health` returns:

```json
{
  "ok": true,
  "service": "orchet-voice",
  "version": "0.1.0",
  "region": "iad",
  "uptime_seconds": 3421,
  "checks": {
    "deepgram_reachable": "ok",
    "daily_reachable": "ok",
    "supabase_jwt_validator": "ok",
    "honeycomb_exporter": "ok"
  }
}
```

Used by Fly health probes and external uptime monitoring.

---

## CI plan (`.github/workflows/ci.yml`)

Runs on every PR and on push to `main`:

1. **Setup** — Python 3.12, install uv, restore uv cache
2. **Install** — `uv sync --frozen`
3. **Lint** — `uv run ruff check .` and `uv run ruff format --check .`
4. **Typecheck** — `uv run pyright voice/ tests/`
5. **Unit tests** — `uv run pytest -v`
6. **Docker build** — confirm the image builds (no push on PR)

Failing CI blocks merge to main.

---

## Deploy pipeline (`.github/workflows/deploy.yml`)

Runs on push to `main` after CI passes:

1. Build Docker image
2. Push to Fly's registry
3. `fly deploy --strategy rolling --regions iad` (Phase 1: single region; Phase 5 expands to multi-region)
4. Health probe — wait for `/health` to return 200 from the new Machine before terminating old one
5. Post-deploy smoke — hit Daily test room, verify connection accepted

Rolling deploy strategy keeps a warm Machine during deploys (no cold-start gap for active voice sessions).

---

## Fly.toml (Phase 1, single region)

```toml
app = "orchet-voice"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[env]
  ORCHET_VOICE_ENV = "production"
  ORCHET_VOICE_REGION = "iad"

[[services]]
  internal_port = 8080
  protocol = "tcp"
  auto_stop_machines = "off"      # always-on for voice — no cold starts
  min_machines_running = 1

  [[services.ports]]
    port = 443
    handlers = ["tls", "http"]

  [[services.tcp_checks]]
    grace_period = "10s"
    interval = "15s"
    timeout = "2s"

[[services.http_checks]]
  path = "/health"
  interval = "30s"
  timeout = "5s"
  method = "GET"

[deploy]
  strategy = "rolling"

[[vm]]
  size = "shared-cpu-2x"
  memory = "1gb"
```

Phase 5 expands `[[vm]]` to additional regions (bom, sin, fra) and tweaks `min_machines_running` per region based on load.

---

## What Phase 1 ships

The above scaffold + a single working flow:

1. Web client connects to `wss://voice.orchet.ai`
2. Bearer JWT validated on signaling
3. Daily WebRTC session opens
4. Pipecat pipeline running with a **single echo processor** (audio in → audio out, no STT/LLM/TTS yet)
5. Round-trip RTT measured and logged to Honeycomb
6. Second Fly Machine deployed to `bom` (Mumbai) — same echo test from a real India endpoint, RTT measured

Phase 2 replaces the echo processor with the real Deepgram → Groq → Aura-2 pipeline.

---

## Out of scope for Phase 0 (this doc) and Phase 1

- No STT / LLM / TTS providers wired (Phase 2)
- No `/voice/turn` calls to backend (Phase 3)
- No Sarvam (Phase 4)
- No multi-region beyond iad + bom probe (Phase 5)
- No iOS WebRTC client (Phase 6)
- No production traffic (Phase 6 launch)

---

## Open questions

1. **Pipecat version pin** — confirm whether the SmallWebRTCTransport regression in 0.0.62+ is fixed yet (May 2026 check). If fixed, pin to latest stable. If not, stay on 0.0.61.
2. **Daily Cloud account ownership** — needs to be under Orchet-AI org/email, not personal. Set up in Phase 1.
3. **Groq account** — same. Provision in Phase 1.
4. **Fly.io Orchet-AI org** — confirm we can create an org-scoped Fly account vs personal. Match the GitHub org pattern.

---

## Phase 1 readiness gate

This scaffold plan is complete. Phase 1 implementation can start once Phase 0 measurement is live (Phase 1 is a parallel lane; doesn't strictly block on measurement, but the measurement baseline informs Phase 2 success criteria).
