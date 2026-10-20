# 22 · Platforms, and what auto-instrumentation gives you

Everything in this series was built by hand so that each piece could be seen: the spans, the collector, the meter, the viewer, the checks, the judge. In practice most teams start from a platform (Phoenix, Langfuse, LangSmith, Braintrust) and an auto-instrumentation library that patches LangGraph and the model SDK with one line each. This stage asks what that one line gets you, on the same agent and the same case.

**The experiment** ([`auto.py`](auto.py)). The grey-zone refund (the shared-device case, which calls the fraud-review sub-agent) runs twice against the platform, in two processes:

- **hand**: the agent's own spans (part 3), following the OpenTelemetry GenAI conventions;
- **auto**: no hand spans at all, only [OpenInference](https://github.com/Arize-ai/openinference) (what Phoenix uses): `LangChainInstrumentor` for the graph and `GoogleGenAIInstrumentor` for the Gemini SDK. No change to the agent's code.

Both send to the same viewer, so the two runs can be read side by side.

## Run it

```bash
make platform
python 22-platforms/auto.py              # both runs, then the comparison
python 22-platforms/auto.py --report     # the comparison again, from the viewer
```

## Real run (gemini-3.5-flash-lite, 2026-10-20)

| | hand | auto |
|---|---|---|
| spans | 45, in **1 trace** | 22, in **7 traces** |
| model calls with token counts | 6 | 6 |
| prompt and response text | 0 (left out on purpose, stage 11) | 22 spans |
| the conversation (session id) | 12 | 16 |
| the decision (action, amount, clause) | 1 | 0 |
| the sub-agent as an agent span | 1 | 0 (a node like any other) |
| platform HTTP calls, tool spans | 14, 8 | 0 |
| guardrail verdicts | 2 | 0 |
| cost per call | 6 | 0 |

What the run taught:

- **Auto-instrumentation saw every model call, and put none of them in the run.** The graph's 16 node spans formed one trace; the 6 Gemini calls came out as 6 separate traces with no parent. Reading the run in a viewer, the model calls simply aren't there, and a cost or latency query per run misses all of them. The cause is in the LangChain instrumentor's source: it records spans from LangChain's callbacks and, as a comment there explains, deliberately doesn't make them the current context, so the Gemini instrumentor has no parent to attach to. Two libraries that each work, combined, give a broken trace. The first thing to check with any auto-instrumentation: is it one tree?
- **It records what's easy to record, not what you'll ask.** Inputs and outputs of every node, as JSON text, which is the most useful thing on day one, and also every customer message in every span (stage 11's redaction would have to run on all of it). What's missing is what this agent is *about*: the decision, the policy clause, the payment, the guardrail verdicts, the cost, that fraud review is a sub-agent. Those come from the code that knows them.
- **The two aren't either-or.** The usual shape is auto for the libraries plus a few hand spans and attributes for the domain, all in one tracer provider so the tree holds together. That's what stages 09–12 built by hand.

Full output: [`output.txt`](output.txt).

## Phoenix and Langfuse

Not run yet: Langfuse Cloud needs an account (the free tier is enough), and Phoenix is a ~400 MB local install. Both accept OTLP, so the plan is the same for each: point the collector's forward exporter (stage 10) at them, send the same two runs, and see what each makes of the GenAI-convention spans versus the OpenInference ones.

## Where it ends

This is the end of the series. Part 1 built the agent, part 2 made it survive production, part 3 made it visible, and part 4 made it judged: deterministic checks, people's labels, a calibrated judge, live checks, curated cases and a gate that blocks a change before it ships. The camera says what happened; the referee says whether it was good.
