# 20 · Online evals and curated cases

Stage 18 judged runs after the fact, in a batch. Production needs the referee on the field: **every run scored as it finishes**, with the scores as numbers over time a dashboard can draw and an alert can watch. And it needs the other direction too: when production finds a failure the case set never imagined, that run should **become a case**, so the fix can be proven and never quietly undone.

**Online evals** ([`evals/online.py`](../evals/online.py)). An evaluator follows the viewer: every trace whose root span has arrived gets the nine checks of stage 18, its results written into the trace as evaluation spans, and a count per check and outcome recorded as a metric (`refund_agent.evaluations`, by `gen_ai.evaluation.name` and `score.label`). In a bigger system the cheap deterministic checks run on every trace and the slow LLM judge (stage 19) on a sample plus everything flagged.

**From a bad run to a case** ([`evals/curate.py`](../evals/curate.py)). A trace says *which* run went wrong; it doesn't hold what the customer wrote (message bodies are off, stage 11). So a curated case is built from the platform's own records: the customer's message from the session, the order as it was, and an expectation set by a reviewer: the decision the policy requires, plus checks on the reply ("amounts in US dollars"). It keeps the order with it (`POST /admin/orders` puts it back), so the case runs the same way months later. It's appended to `evals/curated.jsonl`, next to the hand-written cases, with the trace it came from.

## Run it

```bash
make platform ; make collector
python 20-online-evals/run.py             # online: real runs replayed as traffic, scored as they land
python 20-online-evals/run.py --curate    # live: a flagged run → a case → before and after a fix
```

## Real run (2026-10-20)

**Online.** 150 real runs (the archive of stages 10–12) replayed as live traffic, three a second; the evaluator scored each one as it landed: the first score 1.0 s after the first run, all 150 within 54 s.

| check | pass | fail | pass rate |
|---|---|---|---|
| at_most_one_payment | 95 | 0 | 100% |
| every_call_has_usage | 146 | 0 | 100% |
| output_check_passed | 113 | 2 | 98% |
| sub_agents_finished | 7 | 0 | 100% |
| under_budget | 21 | 0 | 100% |
| verify_loop_bounded | 119 | 0 | 100% |
| within_latency_slo | 110 | 9 | 92% |

Per minute, all checks together: 100%, 100%, 92%. The spend check (stage 18) is left out online: it needs the run's own record, which live traffic doesn't have; online, cost is the meter's job (stage 12).

**Curated** (`--curate`). The Arabic case on live traffic: the first run's reply named riyals again and the online check flagged it. Promoted to a case, `curated-arabic-currency` ([`../evals/curated.jsonl`](../evals/curated.jsonl)): the customer's message and the order from the platform's records, the decision the policy requires (refund $55), and one reviewer's check on the reply: dollars only. Then the case against both compose prompts, five replies each:

| | replies that fail the case |
|---|---|
| compose v1 (today) | **5 of 5** |
| compose v2 (stage 15's fix) | **0 of 5** |

The stage 16 A/B test couldn't prove the fix on live traffic (1 slip in 29 against none in 31). A curated case, aimed at the one place it goes wrong, proved it in ten calls. The case now runs in the release gate's live layer (stage 21), so the riyals can't come back unnoticed. And v2 is now the reply prompt every run uses ([`agentkit/judgments.py`](../agentkit/judgments.py); v1 stays as `COMPOSE_V1` for the A/B arm): the A/B said it was cheaper, this case says it fixes the currency.

Full output: [`output.txt`](output.txt).

## Where it breaks

A curated case and a fix that passes it are only safe until the next change undoes them. Someone edits the compose prompt next month, the riyals come back, and nobody notices until the online check fires in production. The case set has to run *before* a change ships, and block it if the agent got worse. Next: [21 · release gate](../21-release-gate/).
