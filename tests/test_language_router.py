from __future__ import annotations

import pytest

from voice.routing.language_router import (
    detect_language,
    normalize_locale,
    pick_stt_provider,
    pick_tts_provider,
    sarvam_locale_for,
)


@pytest.mark.parametrize(
    ("detected", "locale", "provider"),
    [
        ("en-US", "en-US", "deepgram"),
        ("en-GB", "en-GB", "deepgram"),
        ("en-IN", "en-IN", "deepgram"),
        ("hi", "hi-IN", "sarvam"),
        ("hi-IN", "hi-IN", "sarvam"),
        ("te", "te-IN", "sarvam"),
        ("te-IN", "te-IN", "sarvam"),
        ("ta", "ta-IN", "sarvam"),
        ("ta-IN", "ta-IN", "sarvam"),
        ("hinglish", "hi-IN", "sarvam"),
        ("fr-FR", "fr-FR", "deepgram"),
    ],
)
def test_language_router_provider_decisions(detected: str, locale: str, provider: str) -> None:
    assert normalize_locale(detected) == locale
    assert pick_stt_provider(detected) == provider
    assert pick_tts_provider(detected) == provider


def test_sarvam_locale_for_falls_back_to_hindi() -> None:
    assert sarvam_locale_for("te-IN") == "te-IN"
    assert sarvam_locale_for("en-IN") == "hi-IN"


def test_detect_language_sync_fallback_defaults_to_english() -> None:
    assert detect_language(b"") == "en-US"
