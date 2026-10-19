"""OpenTelemetry for the refund agent, written by hand (stage 09).

One trace per run. Its spans follow the OpenTelemetry GenAI semantic conventions (the
`semantic-conventions-genai` repo, status Development, checked 2026-10-20):

    invoke_workflow refund_agent                      INTERNAL  the run
      node classify                                   INTERNAL  a graph step (our own name)
        chat gemini-3.5-flash-lite                    CLIENT    a model call: tokens, model, finish reason
      node history
        retrieval customer_history                    CLIENT    memory read
      node fraud_review
        invoke_agent fraud_review                     INTERNAL  a sub-agent
          chat gemini-3.5-flash-lite
          execute_tool get_devices                    INTERNAL  a tool the sub-agent called
      node guard
        guardrail prompt_injection                    INTERNAL  verdict as attributes (our own name)

Retries, 429s, fallbacks and guardrail verdicts are span events; failures set the span
status to ERROR with `error.type`. A run that waits for the manager ends its trace there;
the resumed run starts a new trace with a span link back to it (see `resume_link`).

Nothing here depends on a backend: spans go to whatever exporters `setup()` is given. Stage
09 sends them to the platform's viewer; stage 10 adds OTLP and a collector.
"""
import json
import os
import threading
from contextlib import contextmanager

import httpx
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Link, SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

PROVIDER = "gcp.gen_ai"            # gen_ai.provider.name for the Gemini API
TRACER_NAME = "refund-agent"
_provider = None
_lock = threading.Lock()


def span_to_dict(span):
    """A ReadableSpan as plain JSON: what the viewer stores and draws."""
    ctx, parent = span.get_span_context(), span.parent
    return {
        "trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x"),
        "parent_id": format(parent.span_id, "016x") if parent else None,
        "name": span.name, "kind": span.kind.name, "start": span.start_time / 1e9, "end": span.end_time / 1e9,
        "status": span.status.status_code.name, "status_message": span.status.description,
        "attributes": dict(span.attributes or {}),
        "events": [{"name": e.name, "t": e.timestamp / 1e9, "attributes": dict(e.attributes or {})}
                   for e in span.events],
        "links": [{"trace_id": format(link.context.trace_id, "032x"), "span_id": format(link.context.span_id, "016x"),
                   "attributes": dict(link.attributes or {})} for link in span.links],
        "resource": dict(span.resource.attributes),
    }


class ViewerExporter(SpanExporter):
    """Posts finished spans as JSON to the platform (POST /v1/spans), for the live viewer."""

    def __init__(self, url=None):
        self.url = (url or os.environ.get("PLATFORM_URL", "http://127.0.0.1:8710")).rstrip("/") + "/v1/spans"
        self.http = httpx.Client(timeout=3)

    def export(self, spans):
        try:
            self.http.post(self.url, json=[span_to_dict(s) for s in spans])
            return SpanExportResult.SUCCESS
        except httpx.HTTPError:
            return SpanExportResult.FAILURE           # the viewer is optional

    def shutdown(self):
        self.http.close()


def setup(exporters=None, service="refund-agent", environment=None, batch=True, sampler=None):
    """Install a tracer provider once per process. Returns it (tests read its exporters)."""
    global _provider
    with _lock:
        if _provider is not None:
            return _provider
        resource = Resource.create({"service.name": service,
                                    "deployment.environment.name": environment or os.environ.get("AGENT_ENV", "dev")})
        _provider = TracerProvider(resource=resource, **({"sampler": sampler} if sampler else {}))
        for exporter in exporters if exporters is not None else [ViewerExporter()]:
            processor = BatchSpanProcessor(exporter, schedule_delay_millis=500) if batch else SimpleSpanProcessor(exporter)
            _provider.add_span_processor(processor)
        trace.set_tracer_provider(_provider)
        return _provider


def setup_in_memory():
    """For tests: spans kept in memory, exported synchronously."""
    memory = InMemorySpanExporter()
    setup([memory], batch=False)
    return memory


def flush():
    if _provider is not None:
        _provider.force_flush(5000)


def tracer():
    return trace.get_tracer(TRACER_NAME)


def _clean(attrs):
    """OTel attributes are scalars or lists of scalars; anything else becomes JSON."""
    out = {}
    for k, v in (attrs or {}).items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)) or (isinstance(v, (list, tuple)) and
                                                       all(isinstance(x, (str, bool, int, float)) for x in v)):
            out[k] = list(v) if isinstance(v, tuple) else v
        else:
            out[k] = json.dumps(v, ensure_ascii=False, default=str)
    return out


@contextmanager
def span(name, kind=SpanKind.INTERNAL, attributes=None, parent=None, links=None, context=None):
    """Start a span as the current one; ERROR status and error.type if the block raises.
    `context` (a whole OTel context, with baggage) or `parent` (a span) set where it hangs."""
    ctx = context if context is not None else (trace.set_span_in_context(parent) if parent is not None else None)
    with tracer().start_as_current_span(name, context=ctx, kind=kind, attributes=_clean(attributes),
                                        links=links or [], record_exception=False,
                                        set_status_on_exception=False) as s:
        try:
            yield s
        except Exception as e:
            if type(e).__name__ != "GraphInterrupt":     # a pause for a person is not an error
                s.record_exception(e)
                s.set_status(Status(StatusCode.ERROR, f"{type(e).__name__}: {e}"[:200]))
                s.set_attribute("error.type", type(e).__name__)
            raise


def set_attributes(s, attrs):
    s.set_attributes(_clean(attrs))


def event(name, attributes=None, s=None):
    (s or trace.get_current_span()).add_event(name, _clean(attributes))


def fail(s, error_type, message=""):
    s.set_status(Status(StatusCode.ERROR, message[:200]))
    s.set_attribute("error.type", error_type)


def current():
    return trace.get_current_span()


def in_trace():
    """True inside a run's trace. Calls made outside any run (resets, health checks, the
    manager's console) don't start traces of their own."""
    return trace.get_current_span().get_span_context().is_valid


def capture_content():
    """Message bodies on spans are opt-in (GenAI conventions): off unless asked for."""
    return os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "NO_CONTENT").upper() in (
        "SPAN_ONLY", "SPAN_AND_EVENT", "TRUE")


def conversation_id():
    from opentelemetry import baggage
    return baggage.get_baggage("gen_ai.conversation.id")


def attach(span_obj):
    """Make a span current in this thread (LangGraph runs nodes on worker threads)."""
    return otel_context.attach(trace.set_span_in_context(span_obj))


def detach(token):
    otel_context.detach(token)


def carrier():
    """The current span as a W3C traceparent, to store with a checkpoint."""
    c = {}
    TraceContextTextMapPropagator().inject(c)
    return c


def resume_link(stored):
    """A link to the span that paused, from its stored traceparent."""
    ctx = trace.get_current_span(TraceContextTextMapPropagator().extract(stored)).get_span_context()
    return [Link(ctx, {"link.reason": "resumed after waiting for the operations manager"})] if ctx.is_valid else []


def ids(s):
    c = s.get_span_context()
    return format(c.trace_id, "032x"), format(c.span_id, "016x")
