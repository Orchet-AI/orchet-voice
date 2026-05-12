from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from voice.obs.logging import configure_logging
from voice.obs.tracing import configure_tracing
from voice.routes import debug, health, internal
from voice.settings import Settings
from voice.transport import VoiceSessionManager

# Browser clients (orchet.ai web app) call /debug/echo cross-origin with
# an Authorization header. That triggers a CORS preflight. Without this
# middleware FastAPI 405s the OPTIONS request and the browser blocks the
# POST. The Vercel preview-deploy regex covers PR previews so
# soak/staging flows work too. Auth is the Supabase JWT in the
# Authorization header — no cookies — so allow_credentials stays False.
ALLOWED_ORIGINS = [
    "https://orchet.ai",
    "https://www.orchet.ai",
    "http://localhost:3000",
]
ALLOWED_ORIGIN_REGEX = (
    r"https://(lumo-super-agent|orchet-web|orchet-app)"
    r"(-[a-z0-9-]+)?\.vercel\.app"
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.voice_sessions = VoiceSessionManager(app.state.settings)
    yield
    await app.state.voice_sessions.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    resolved_settings = settings or Settings.from_env()
    app = FastAPI(title="orchet-voice", version=resolved_settings.version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_origin_regex=ALLOWED_ORIGIN_REGEX,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
        max_age=86400,  # cache preflight for 24h
    )
    app.state.settings = resolved_settings
    app.include_router(health.router)
    app.include_router(debug.router)
    app.include_router(internal.router)
    configure_tracing(app, resolved_settings)
    return app


app = create_app()
