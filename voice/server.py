from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from voice.obs.logging import configure_logging
from voice.obs.tracing import configure_tracing
from voice.routes import debug, health
from voice.settings import Settings
from voice.transport import EchoSessionManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.echo_sessions = EchoSessionManager(app.state.settings)
    yield
    await app.state.echo_sessions.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    resolved_settings = settings or Settings.from_env()
    app = FastAPI(title="orchet-voice", version=resolved_settings.version, lifespan=lifespan)
    app.state.settings = resolved_settings
    app.include_router(health.router)
    app.include_router(debug.router)
    configure_tracing(app, resolved_settings)
    return app


app = create_app()
