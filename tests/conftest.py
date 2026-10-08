import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_runtime import telemetry
from agent_runtime.persistence import open_persistence
from agent_runtime.providers import FailoverRouter, MockProvider
from agent_runtime.service import RunManager


class Telemetry:
    def __init__(self):
        self.reader = InMemoryMetricReader()
        self.spans = InMemorySpanExporter()
        tp = TracerProvider()
        tp.add_span_processor(SimpleSpanProcessor(self.spans))
        telemetry.configure(MeterProvider(metric_readers=[self.reader]), tp)

    def points(self, name):
        data = self.reader.get_metrics_data()
        out = []
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    if m.name == name:
                        out.extend(m.data.data_points)
        return out

    def total(self, name, **attrs):
        return sum(
            getattr(p, "value", None) if hasattr(p, "value") else p.sum
            for p in self.points(name)
            if all(p.attributes.get(k) == v for k, v in attrs.items())
        )

    def span_names(self):
        return [s.name for s in self.spans.get_finished_spans()]


@pytest.fixture
def otel():
    t = Telemetry()
    yield t
    telemetry.configure()


def make_router(primary="none", backup="none"):
    return FailoverRouter([
        MockProvider(name="mock-primary", model="mock-small", fail_mode=primary),
        MockProvider(name="mock-backup", model="mock-large", fail_mode=backup),
    ])


@pytest.fixture
async def persistence():
    async with open_persistence("memory") as p:
        yield p


@pytest.fixture
async def manager(persistence, otel):
    return RunManager(persistence, make_router())


def pytest_collection_modifyitems(config, items):
    import os
    if os.getenv("AGENTRT_PG_DSN"):
        return
    skip = pytest.mark.skip(reason="set AGENTRT_PG_DSN to run Postgres tests")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)
