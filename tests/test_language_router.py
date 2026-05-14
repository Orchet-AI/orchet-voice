from __future__ import annotations

import pytest

from voice.routing.language_router import (
    SARVAM_WEAK_CONFIDENCE_THRESHOLD,
    detect_language,
    normalize_locale,
    override_weak_sarvam_to_english,
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


@pytest.mark.parametrize(
    ("locale", "confidence", "expected"),
    [
        # Sarvam-routed locale with sub-threshold confidence → override
        # to en-US. This is the load-bearing case: Honeycomb showed
        # AVG(voice.detect.confidence) = 0 across all 406 detections,
        # so every non-English Sarvam result lands here.
        ("hi-IN", 0.0, "en-US"),
        ("hi-IN", 0.3, "en-US"),
        ("hi-IN", 0.49, "en-US"),
        ("ta-IN", 0.2, "en-US"),
        ("te-IN", 0.1, "en-US"),
        # Sarvam-routed locale with high confidence → respect Sarvam
        # routing. Legitimate Hindi/Tamil/Telugu speakers don't get
        # incorrectly dropped to English when Sarvam IS sure.
        ("hi-IN", 0.8, "hi-IN"),
        ("ta-IN", 0.95, "ta-IN"),
        ("te-IN", 0.7, "te-IN"),
        # English locales are always returned as-is regardless of
        # confidence — the override is one-directional.
        ("en-US", 0.0, "en-US"),
        ("en-IN", 0.1, "en-IN"),
        ("en-GB", 0.99, "en-GB"),
        # Non-Sarvam, non-English locales (e.g. French) pass through
        # untouched — the override only fires when the would-be route
        # is Sarvam.
        ("fr-FR", 0.0, "fr-FR"),
        ("ja-JP", 0.0, "ja-JP"),
    ],
)
def test_override_weak_sarvam_to_english(
    locale: str, confidence: float, expected: str
) -> None:
    assert override_weak_sarvam_to_english(locale, confidence) == expected


def test_override_threshold_is_exclusive_lower_bound() -> None:
    """Confidence exactly at the threshold should respect Sarvam (not
    override). Strictly-less-than semantics — the rule is 'when we
    don't have at least the threshold of confidence'."""
    assert (
        override_weak_sarvam_to_english(
            "hi-IN", SARVAM_WEAK_CONFIDENCE_THRESHOLD
        )
        == "hi-IN"
    )
    assert (
        override_weak_sarvam_to_english(
            "hi-IN", SARVAM_WEAK_CONFIDENCE_THRESHOLD - 0.01
        )
        == "en-US"
    )


def test_override_respects_custom_threshold() -> None:
    """Operators can override the threshold per-deploy. A stricter
    threshold biases more aggressively to English; a looser threshold
    trusts Sarvam more."""
    # Stricter: even high-confidence Sarvam routes go to English.
    assert (
        override_weak_sarvam_to_english("hi-IN", 0.85, threshold=0.9)
        == "en-US"
    )
    # Looser: even very low Sarvam confidence is respected.
    assert (
        override_weak_sarvam_to_english("hi-IN", 0.1, threshold=0.05)
        == "hi-IN"
    )
