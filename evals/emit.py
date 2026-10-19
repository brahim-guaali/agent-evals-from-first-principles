"""Evaluation results back into the trace (stage 18), as the GenAI conventions describe: an
event `gen_ai.evaluation.result` (name, score.value, score.label, explanation) parented to the
evaluated span. The run's spans are finished, so each result travels on a short span of its
own, `evaluate <check>`, created as a child of the run's root span: it lands in the same
trace, next to what it judged.
"""
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from agentkit import telemetry


def emit(trace_id, root_span_id, results, evaluator="deterministic"):
    parent = NonRecordingSpan(SpanContext(int(trace_id, 16), int(root_span_id, 16), is_remote=True,
                                          trace_flags=TraceFlags(TraceFlags.SAMPLED)))
    ctx = trace.set_span_in_context(parent)
    for name, r in results.items():
        with telemetry.span(f"evaluate {name}", context=ctx, attributes={"gen_ai.evaluation.name": name,
                                                                          "refund_agent.evaluator": evaluator}) as s:
            telemetry.event("gen_ai.evaluation.result", {
                "gen_ai.evaluation.name": name, "gen_ai.evaluation.score.value": 1.0 if r["passed"] else 0.0,
                "gen_ai.evaluation.score.label": "pass" if r["passed"] else "fail",
                "gen_ai.evaluation.explanation": r["explanation"]}, s)
            if not r["passed"]:
                telemetry.fail(s, "evaluation_failed", r["explanation"])
