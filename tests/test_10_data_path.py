import threading
import time

import httpx
import pytest
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from conftest import _free_port


@pytest.fixture(scope="module")
def collector(platform_url, tmp_path_factory):
    import os
    os.environ["COLLECTOR_ARCHIVE"] = str(tmp_path_factory.mktemp("archive"))
    import uvicorn
    from services import collector as c
    c.PLATFORM, c.ARCHIVE = platform_url, __import__("pathlib").Path(os.environ["COLLECTOR_ARCHIVE"])
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(c.app, port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url + "/health")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield url
    server.should_exit = True


def test_spans_go_through_otlp_and_the_collector_to_the_viewer_and_the_archive(platform, collector):
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{collector}/v1/traces")))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("invoke_workflow refund_agent", attributes={"refund_agent.run_id": "dp1"}):
        with tracer.start_as_current_span("chat gemini", attributes={"gen_ai.usage.input_tokens": 12}):
            pass
    provider.shutdown()
    time.sleep(1.6)                                         # the batch processor flushes every second
    traces = httpx.get(f"{platform}/traces").json()
    t = next(t for t in traces if t["title"].startswith("dp1"))
    spans = httpx.get(f"{platform}/traces/{t['trace_id']}").json()["spans"]
    assert {s["name"] for s in spans} == {"invoke_workflow refund_agent", "chat gemini"}
    assert next(s for s in spans if s["name"] == "chat gemini")["attributes"]["gen_ai.usage.input_tokens"] == 12
    stats = httpx.get(f"{collector}/stats").json()
    assert stats["received.spans"] >= 2 and stats["exported.viewer"] >= 2 and stats["exported.file"] >= 2


def test_metrics_reach_the_dashboard_as_percentiles(platform, collector):
    reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=f"{collector}/v1/metrics",
                                                              preferred_temporality={Histogram: AggregationTemporality.DELTA}),
                                           export_interval_millis=60_000)
    from agentkit.telemetry import _views
    provider = MeterProvider(metric_readers=[reader], views=_views())     # the GenAI buckets, as the agent uses
    h = provider.get_meter("test").create_histogram("gen_ai.client.inference.duration", unit="s")
    for v in [0.5] * 90 + [8.0] * 10:
        h.record(v, {"gen_ai.request.model": "m"})
    provider.force_flush()
    provider.shutdown()
    rows = httpx.get(f"{platform}/metrics/series", params={"name": "gen_ai.client.inference.duration"}).json()
    assert rows[-1]["count"] == 100 and rows[-1]["p50"] < 1 and 5 < rows[-1]["p95"] < 11


def test_percentiles_interpolate_inside_a_bucket():
    from services.app import percentile
    bounds, buckets = [1, 2, 4], [0, 10, 0, 0]           # ten values between 1 and 2
    assert percentile(bounds, buckets, .5) == pytest.approx(1.5)
    assert percentile(bounds, [0, 0, 0, 0], .5) is None
