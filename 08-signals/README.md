# 08 · Signals

Two things a run can't know about itself: what the customer thought of the answer, and *what exactly* produced it. Both come from outside the run, and both are what you need when something changes. Was last week's drop in ratings the new prompt? The model Google rolled out on Tuesday? A config change in staging?

**Ratings.** The platform takes `POST /feedback {run_id, customer, rating 1–5, comment}`. A rating arrives after the run, from the customer's app, and is stored against the `run_id` that produced the reply. Joining it back is the whole trick. The customers in our traffic are simulated, so their ratings are too, by a rule written out in [`agentkit/customers.py`](../agentkit/customers.py): about half rate, they start from what they got (full refund 5, credit 3, refusal 1…), and lose a star for a wait over 60 s and another over 120 s, and one for a correction message.

**Versions.** Every run now carries `meta` ([`agent/versions.py`](../agent/versions.py)):

| Field | What it is |
|---|---|
| `prompts` | an 8-character hash of every prompt: classify, decide, explain, compose, the two sub-agents, support, the injection check |
| `prompt_set` | a hash of all of them: same value, same instructions |
| `agent` | the git commit of the agent code (`-dirty` if it has uncommitted changes) |
| `environment` | `dev`, `staging` or `prod` (`AGENT_ENV`) |
| `model_versions` | what Gemini says answered (`response.model_version`), not only what we asked for |

Change one word in one prompt and only that prompt's hash and the set's hash change. That's how a dashboard gets filtered by version, and how the A/B test in stage 16 tells its arms apart.

## Run it

```bash
make platform
GEMINI_RPM=12 python 08-signals/run.py 40          # traffic with ratings, environment "staging"
```

## Real run (gemini-3.1-flash-lite, 2026-10-20)

40 requests in staging, 4 a minute, 3 workers ([`data/`](data/)):

| | |
|---|---|
| Rated | **22 of 40** customers (55%), average **2.86★**, all 22 joined back to their run by `run_id` |
| By outcome | refund 3.4★ (14) · credit 2.5★ · escalated 2.5★ · referred to a person 3.0★ · **refused 1.0★ (3)** |
| By wait | under 60 s 3.3★ · 60–120 s 3.7★ · **over 120 s 2.2★ (12)** |
| Versions | one combination all batch: agent `bb2b102-dirty`, prompt set `944bc7f7`, staging; **every call answered by `gemini-3.1-flash-lite`** (3.5 Flash-Lite's daily quota was already spent) |
| Runs | 35/40 graded right · run p50 46.8 s · queue p50 83 s · customer wait p50 133 s |

The lowest-rated runs, as a dashboard would list them:

```
1★ "Useless"                      refuse $0 under §7   154 s wait   prompts 944bc7f7   gemini-3.1-flash-lite
1★ "Never ordering again"         refuse $0 under §7   172 s wait
1★ "Unfair, I'm leaving the app"  refuse $0 under §7   140 s wait
1★ "Useless"                      credit $0 under §6   170 s wait
2★ "Took too long"                refund $560 under §1 153 s wait
```

What the numbers say:

- **A correct answer can still be a 1★.** The three refusals were right (the order had already been refunded) and rated 1. Ratings measure the customer's happiness, not the agent's correctness; you need both, and you can't infer one from the other.
- **Waiting costs more stars than anything the agent decides.** Over 120 s, refunds were rated 2★ too. In this batch the wait was mostly the queue, again.
- **Three of the five "wrong" runs were the grader's mistake.** When a customer sent the same message twice, the second copy was sometimes served *before* the first: the repeat got the refund and the original was refused. One refund per order, exactly as the policy says, but the grader expected the first-arrived message to get it. The other two were injected bad data, escalated as they should be. Under concurrency, "the expected answer" depends on order; graders need the same care as the agent (part 3).
- **The version tags already earned their keep.** Every rating says which prompts and model produced it. If 3.1 Flash-Lite answers worse than 3.5, this is how you'd see it.

Full output: [`output.txt`](output.txt).

## Where it breaks

Part 1 is done: the agent remembers, converses, delegates, streams, fails gracefully, has guardrails, takes traffic and carries its versions. And we can say less about it than ever. The log has one line per run: a total time, a cost, a list of events. When a run took 93 s instead of 22, the line can't say which step, which call, which retry. When a rating is 1★, it can't show what the customer saw happen. Everything we printed so far was assembled by hand for one demo. Part 2 builds the camera. Next: [09 · spans](../09-spans/).
