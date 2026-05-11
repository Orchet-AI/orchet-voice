from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from voice.settings import Settings

router = APIRouter()
_started_at = time.monotonic()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return {
        "ok": True,
        "service": "orchet-voice",
        "version": settings.version,
        "region": settings.region,
        "uptime_seconds": int(time.monotonic() - _started_at),
        "checks": settings.health_checks(),
    }
