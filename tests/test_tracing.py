from __future__ import annotations

from voice.obs.tracing import normalize_otlp_endpoint, parse_otlp_headers


def test_parse_otlp_headers() -> None:
    assert parse_otlp_headers("x-honeycomb-team=abc, x-honeycomb-dataset=voice") == {
        "x-honeycomb-team": "abc",
        "x-honeycomb-dataset": "voice",
    }


def test_normalize_honeycomb_base_endpoint_to_otlp_traces_path() -> None:
    assert normalize_otlp_endpoint("https://api.honeycomb.io") == (
        "https://api.honeycomb.io/v1/traces"
    )


def test_normalize_otlp_endpoint_preserves_explicit_paths() -> None:
    assert normalize_otlp_endpoint("https://example.com/v1/traces") == (
        "https://example.com/v1/traces"
    )
