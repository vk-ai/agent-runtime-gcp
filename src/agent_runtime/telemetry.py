"""OpenTelemetry metrics and traces for agent runs.

Instruments are created against an explicit MeterProvider/TracerProvider so
tests can plug in in-memory readers. `configure_from_env()` wires console or
OTLP/HTTP exporters for real use.
"""
from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field

from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.metrics import CallbackOptions, Observation


@dataclass
class _State:
    meter_provider: otel_metrics.MeterProvider = field(default_factory=otel_metrics.NoOpMeterProvider)
    tracer_provider: otel_trace.TracerProvider = field(default_factory=otel_trace.NoOpTracerProvider)
    stuck_runs: int = 0
    waiting_approval: int = 0
    instruments: dict = field(default_factory=dict)


_state = _State()


def _observe_stuck(_: CallbackOptions):
    yield Observation(_state.stuck_runs)


def _observe_waiting(_: CallbackOptions):
    yield Observation(_state.waiting_approval)


def configure(meter_provider=None, tracer_provider=None) -> None:
    """Install providers and (re)create all instruments."""
    global _state
    _state = _State(
        meter_provider=meter_provider or otel_metrics.NoOpMeterProvider(),
        tracer_provider=tracer_provider or otel_trace.NoOpTracerProvider(),
    )
    m = _state.meter_provider.get_meter("agent_runtime")
    _state.instruments = {
        "runs_started": m.create_counter("agent_runs_started", unit="1", description="Runs started"),
        "runs_finished": m.create_counter(
            "agent_runs_finished", unit="1", description="Runs finished, by status (succeeded/failed/rejected)"
        ),
        "run_cost": m.create_histogram("agent_run_cost_usd", unit="USD", description="LLM cost per finished run"),
        "run_duration": m.create_histogram("agent_run_duration_s", unit="s", description="Wall time per finished run"),
        "tokens": m.create_counter("agent_llm_tokens", unit="1", description="LLM tokens by provider/model/kind"),
        "provider_failures": m.create_counter(
            "agent_provider_failures", unit="1", description="Provider call failures by provider/reason"
        ),
        "failovers": m.create_counter("agent_provider_failovers", unit="1", description="Calls served by a fallback"),
        "resumes": m.create_counter("agent_runs_resumed", unit="1", description="Runs resumed from a checkpoint"),
    }
    m.create_observable_gauge(
        "agent_runs_stuck", callbacks=[_observe_stuck], unit="1",
        description="Runs marked running with no heartbeat for longer than the stuck threshold",
    )
    m.create_observable_gauge(
        "agent_runs_waiting_approval", callbacks=[_observe_waiting], unit="1",
        description="Runs paused at the human-approval interrupt",
    )


def configure_from_env() -> None:  # pragma: no cover - exercised manually / in compose
    """AGENTRT_OTEL=console|otlp|off (default off)."""
    mode = os.getenv("AGENTRT_OTEL", "off").lower()
    if mode == "off":
        configure()
        return
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "agent-runtime")})
    if mode == "otlp":
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        mexp, sexp = OTLPMetricExporter(), OTLPSpanExporter()
    else:
        mexp, sexp = ConsoleMetricExporter(), ConsoleSpanExporter()
    interval = int(os.getenv("AGENTRT_OTEL_INTERVAL_MS", "15000"))
    mp = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(mexp, interval)])
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(sexp))
    configure(mp, tp)


def tracer():
    return _state.tracer_provider.get_tracer("agent_runtime")


@contextlib.contextmanager
def span(name: str, **attrs):
    with tracer().start_as_current_span(name, attributes={k: v for k, v in attrs.items() if v is not None}) as s:
        yield s


def _inst(name):
    if not _state.instruments:
        configure(_state.meter_provider, _state.tracer_provider)
    return _state.instruments[name]


def record_run_started(run_id: str) -> None:
    _inst("runs_started").add(1)


def record_run_finished(status: str, cost_usd: float, duration_s: float, provider: str | None) -> None:
    attrs = {"status": status, "provider": provider or "none"}
    _inst("runs_finished").add(1, attrs)
    _inst("run_cost").record(cost_usd, attrs)
    _inst("run_duration").record(duration_s, attrs)


def record_resume() -> None:
    _inst("resumes").add(1)


def record_tokens(provider: str, model: str, prompt: int, completion: int) -> None:
    _inst("tokens").add(prompt, {"provider": provider, "model": model, "kind": "prompt"})
    _inst("tokens").add(completion, {"provider": provider, "model": model, "kind": "completion"})


def record_provider_failure(provider: str, reason: str) -> None:
    _inst("provider_failures").add(1, {"provider": provider, "reason": reason})


def record_failover(primary: str, served_by: str) -> None:
    _inst("failovers").add(1, {"primary": primary, "served_by": served_by})


def set_run_gauges(stuck: int, waiting_approval: int) -> None:
    _state.stuck_runs = stuck
    _state.waiting_approval = waiting_approval


def shutdown() -> None:
    """Flush exporters (called on app shutdown so the last spans/metrics are not lost)."""
    for p in (_state.tracer_provider, _state.meter_provider):
        fn = getattr(p, "shutdown", None)
        if fn:
            fn()
