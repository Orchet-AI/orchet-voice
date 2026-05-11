from __future__ import annotations

from urllib.parse import urlparse

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from voice.settings import Settings

TRACER_NAME = "orchet.voice"
_configured = False


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def configure_tracing(app: FastAPI, settings: Settings) -> None:
    global _configured

    if _configured or not settings.otel_endpoint:
        return

    resource = Resource.create(
        {
            "service.name": "orchet-voice",
            "service.version": settings.version,
            "deployment.environment": settings.environment,
            "fly.region": settings.region,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=normalize_otlp_endpoint(settings.otel_endpoint),
                headers=parse_otlp_headers(settings.otel_headers),
            )
        )
    )
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    _configured = True


def parse_otlp_headers(raw_headers: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in raw_headers.split(","):
        key, separator, value = item.strip().partition("=")
        if separator and key and value:
            headers[key] = value
    return headers


def normalize_otlp_endpoint(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.netloc == "api.honeycomb.io" and parsed.path in {"", "/"}:
        return "https://api.honeycomb.io/v1/traces"
    return endpoint
