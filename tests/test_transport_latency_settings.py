from __future__ import annotations

from voice.transport import build_voice_pipeline_params


def test_voice_pipeline_params_enable_pipecat_metrics(settings) -> None:
    params = build_voice_pipeline_params(settings)

    assert params.enable_metrics is True
    assert params.enable_usage_metrics is True
