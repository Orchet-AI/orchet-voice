from __future__ import annotations

from voice.routing.language_router import SARVAM_ROUTED_LOCALES, normalize_locale

SARVAM_PREFERRED_REGIONS: tuple[str, ...] = ("bom", "sin")


def should_migrate_for_sarvam(current_region: str, detected_locale: str) -> bool:
    """Return True when Sarvam audio should move closer to the India API path."""
    region = current_region.strip().lower()
    locale = normalize_locale(detected_locale)
    return locale in SARVAM_ROUTED_LOCALES and region not in SARVAM_PREFERRED_REGIONS


def pick_target_region(current_region: str) -> str:
    """Return BOM as primary, SIN as fallback when already on BOM."""
    return "sin" if current_region.strip().lower() == "bom" else "bom"
