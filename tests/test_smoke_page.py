from __future__ import annotations

from pathlib import Path


def test_smoke_page_documents_echo_flow_without_committed_tokens() -> None:
    html = Path("tests/smoke/web-client.html").read_text(encoding="utf-8")

    assert "DailyIframe" in html
    assert "/debug/echo" in html
    assert "Authorization" in html
    assert "Bearer" in html
    assert "replace-with" not in html
