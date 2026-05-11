from __future__ import annotations

from pathlib import Path

from voice.routing.language_router import load_voice_prompt


def test_per_language_prompt_loading() -> None:
    assert "Orchet" in load_voice_prompt("hi-IN")
    assert "Orchet" in load_voice_prompt("te-IN")
    assert "Orchet" in load_voice_prompt("ta-IN")


def test_prompt_loading_falls_back_to_english_for_missing_locale(tmp_path: Path) -> None:
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "default_voice.txt").write_text("English fallback", encoding="utf-8")

    assert load_voice_prompt("fr-FR", prompt_dir=prompt_dir) == "English fallback"
