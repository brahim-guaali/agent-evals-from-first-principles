# 00 · The starting point

This course begins where [part 3](https://github.com/brahim-guaali/agent-observability-from-first-principles) ends. The refund agent in [`agent/`](../agent/) is already production-shaped (part 2: memory, conversations, sub-agents, streaming, failures, guardrails, traffic, signals) and fully observable (part 3: OpenTelemetry GenAI spans, a collector, metrics, redaction, a metering proxy, sampling, alerts, replay from checkpoints, A/B tests).

What it can't do yet is say whether a run was *good*. That's this course: the referee.

| What you get from earlier parts | Where |
|---|---|
| The agent (LangGraph graph, verifier, sub-agents, guardrails) | [`agent/`](../agent/) |
| Gemini client, platform client, cases, traffic replayer, telemetry | [`agentkit/`](../agentkit/) |
| The platform, the viewer (`/ui/traces`), the dashboard, the collector, the meter | [`services/`](../services/) |
| Real recorded runs: traffic logs from stages 07, 08, 10, 12 and the spans of stages 10 and 12 | [`history/`](../history/) |

```bash
make setup
make platform ; make collector          # two terminals
```

Next: [17 · annotation](../17-annotation/).
