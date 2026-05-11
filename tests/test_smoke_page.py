from __future__ import annotations

from pathlib import Path


def test_smoke_page_documents_voice_flow_without_committed_tokens() -> None:
    html = Path("tests/smoke/web-client.html").read_text(encoding="utf-8")

    assert "DailyIframe" in html
    assert "/debug/echo" in html
    assert "@ricky0123/vad-web" in html
    assert "vad.MicVAD" in html
    assert "barge_in" in html
    assert "Authorization" in html
    assert "Bearer" in html
    assert "replace-with" not in html
