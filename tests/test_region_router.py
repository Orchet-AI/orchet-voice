from __future__ import annotations

import pytest

from voice.routing.region_router import pick_target_region, should_migrate_for_sarvam


@pytest.mark.parametrize(
    ("current_region", "detected_locale", "expected"),
    [
        ("iad", "hi-IN", True),
        ("fra", "te-IN", True),
        ("iad", "ta-IN", True),
        ("iad", "hinglish", True),
        ("bom", "hi-IN", False),
        ("sin", "te-IN", False),
        ("iad", "en-US", False),
        ("fra", "en-IN", False),
    ],
)
def test_should_migrate_for_sarvam(
    current_region: str,
    detected_locale: str,
    expected: bool,
) -> None:
    assert should_migrate_for_sarvam(current_region, detected_locale) is expected


def test_pick_target_region_prefers_bom_with_sin_fallback() -> None:
    assert pick_target_region("iad") == "bom"
    assert pick_target_region("fra") == "bom"
    assert pick_target_region("bom") == "sin"
