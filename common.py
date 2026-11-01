"""Shared OpenTelemetry setup for every process in the demo.

Each process (the agent and the four HTTP services) gets its own TracerProvider with
`service.name` set, and writes every finished span as one JSON line (the OpenTelemetry
Python SDK's own `ReadableSpan.to_json()`) to `<out>/spans-<service>.jsonl`.
No SaaS, no collector: the files are merged into one trace afterwards by `run_demo.py`.

W3C trace context: the SDK's default global propagator is `tracecontext,baggage`, so the
instrumented httpx client injects `traceparent` on every outgoing request and the
instrumented FastAPI server extracts it — that is the whole propagation mechanism.
"""
from __future__ import annotations

import json
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

# Stable HTTP semantic conventions (http.request.method, url.full, error.type …) in the
# contrib instrumentations, instead of the legacy http.* names.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

PORTS = {"orders-svc": 7611, "refunds-svc": 7612, "payments-svc": 7613, "notify-svc": 7614}
LLM_BASE_URL = "http://127.0.0.1:4000"  # local Anthropic-compatible endpoint (serves Kimi K3)
MODEL = "kimi-k3"


def url(service: str, path: str) -> str:
    return f"http://127.0.0.1:{PORTS[service]}{path}"


def setup_tracing(service: str) -> TracerProvider:
    out = os.environ["DEMO_OUT"]
    os.makedirs(out, exist_ok=True)
    sink = open(os.path.join(out, f"spans-{service}.jsonl"), "a", buffering=1, encoding="utf-8")
    provider = TracerProvider(resource=Resource.create({"service.name": service, "deployment.environment.name": "demo"}))
    provider.add_span_processor(
        SimpleSpanProcessor(ConsoleSpanExporter(out=sink, formatter=lambda s: s.to_json(indent=None) + "\n"))
    )
    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    def request_hook(span, request) -> None:
        # Retries are numbered by the caller in a header; record them with the stable
        # semconv attribute so a trace query can count resends.
        attempt = request.headers.get("x-attempt")
        if attempt and int(attempt) > 1 and span.is_recording():
            span.set_attribute("http.request.resend_count", int(attempt) - 1)

    HTTPXClientInstrumentor().instrument(tracer_provider=provider, request_hook=request_hook)
    return provider


def instrument_fastapi(app, provider: TracerProvider) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="health",
        exclude_spans=["receive", "send"],
        # Record the propagated header itself on each server span, so the trace shows it.
        http_capture_headers_server_request=["traceparent"],
    )


def auth_token() -> str:
    """Read the local endpoint's token at runtime. Never printed, never written."""
    with open(os.path.expanduser("~/.claude/settings.json"), encoding="utf-8") as f:
        return json.load(f)["env"]["ANTHROPIC_AUTH_TOKEN"]


def llm_client(provider: TracerProvider, content: bool):
    """Anthropic Python SDK client. Since the OpenTelemetry beta (14 Oct 2026) the SDK emits
    `anthropic.messages.create` / `anthropic.messages.tool_runner` / `anthropic.custom_tool_use`
    spans with gen_ai.* attributes as soon as a tracer provider is given."""
    import anthropic

    return anthropic.Anthropic(
        base_url=LLM_BASE_URL,
        auth_token=auth_token(),
        api_key=None,
        max_retries=1,
        timeout=120,
        open_telemetry={
            "tracer_provider": provider,
            "traces": {"content_mode": "content" if content else "metadata_only"},
        },
    )
