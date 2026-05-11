from __future__ import annotations

from fastapi.testclient import TestClient

from voice.server import create_app
from voice.settings import Settings


def test_health_shape(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["service"] == "orchet-voice"
    assert payload["version"] == "0.1.0"
    assert payload["region"] == "iad"
    assert isinstance(payload["uptime_seconds"], int)
    assert payload["checks"] == {
        "deepgram_reachable": "ok",
        "daily_reachable": "ok",
        "supabase_jwt_validator": "ok",
        "honeycomb_exporter": "missing",
    }
