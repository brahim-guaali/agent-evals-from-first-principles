# 19 · The judge, and how far to trust it

Stage 17's labels took an afternoon for 30 runs. A model that answers the same four questions could read every run, but only once we know where it agrees with the labels and where it doesn't. So the judge is **calibrated before its verdicts count** ([`evals/judge.py`](../evals/judge.py)):

- **The same questions, the same snapshot.** The judge (Gemini) gets exactly what the annotation page shows: the message, the facts, the policy and FAQ, the decision and the reply. It answers in a fixed shape: reasoning, three yes/no questions and a tone.
- **Agreement and Cohen's kappa per question.** Agreement alone flatters a judge when most answers are "yes": a judge that always says yes agrees 90% with labels that are 90% yes. Kappa is agreement beyond what chance would give (0 = chance, 1 = perfect). When every label is the same, kappa can't be computed, and the script says so instead of printing a number.
- **Factual and judgement questions reported apart**, because their labels come from different places (stage 17): the known right answers for the factual ones, a second model with a written rubric for the judgement ones.
- **Every disagreement goes back to the queue** for a second look.
- **Position bias.** Asked which of two replies is better, does the judge's answer change when their order is swapped? Each pair is asked twice.

## Run it

```bash
make platform
python 19-judge/run.py                        # against the reference labels (stage 17)
python 19-judge/run.py --annotator <name>     # against a person's labels from the queue
```

## Real run (gemini-3.5-flash-lite, 2026-10-22)

The judge on the 30 labelled runs, then 12 pairs for position bias: 66 calls, $0.024 at list price.

| question | labels from | agreement | kappa |
|---|---|---|---|
| decision correct | the known right answers | 29/30 (97%) | n/a: every label is "yes" |
| amount and currency right | the known right answers | 30/30 (100%) | 1.00 |
| reply grounded | a second model, written rubric | 22/30 (73%) | 0.37 |
| tone | a second model, written rubric | 25/30 (83%) | |

Position bias: **none** in 12 pairs. The judge picked the same reply both times in every pair, and the reply shown first won exactly half the time.

What the run taught:

- **On facts, the judge is reliable.** It found the one reply in riyals and agreed on every amount. Its one "wrong decision" was a gap in what it was shown, not a bad read: the frequent claimer's refund went through after a manager approved it (§8), but the snapshot only holds the decision, which cites §2. Shown only that, the judge was right to object. The snapshot should carry the approval.
- **On grounding, the judge and the rubric split on one rule.** The judge caught every "3 to 5 business days" against the FAQ's 5 to 10. It let through all 8 "your credit is ready to use right now" (the FAQ says within an hour), and the rider reply's "we'll make sure this doesn't happen again". Kappa 0.37 is that split: the judge reads "right now" as harmless, the rubric doesn't. Who is right is a policy decision, and it belongs in the judge's prompt, not in its head.
- **On tone, the judge was kinder to refusals.** The three replies the rubric called "ok", all refusals with little or no reason, the judge called "good". It called two refund replies "ok" instead.
- **A judge with no position bias still has preferences.** In 11 of 12 pairs it preferred the original reply, the longer one, over the shorter compose v2 reply the A/B test chose for cost (part 3). Asked which is better, "longer" often wins.

**How strong is this evidence?** The factual labels are real reference labels. The judgement labels come from another model, so a 73% agreement there is one model against another, weaker than agreement with people. A person-labelled set (the same queue, `--annotator <name>`) is the next step before the judge's grounding and tone verdicts are trusted on their own.

**The approval, added (2026-10-22).** The snapshot now carries `manager_approval` ([`agentkit/annotation.py`](../agentkit/annotation.py)), the judge's prompt says a §3 or §8 refund is right once a manager approved it, and the queue page shows it. A fresh frequent-claimer run (refund $15, approved), judged three times each way (`python 19-judge/run.py --approval`, [`output-approval.txt`](output-approval.txt)):

| snapshot | judge says the decision is correct |
|---|---|
| without the approval (as in stage 17) | 0 of 3 |
| with the approval | 3 of 3 |

Full output: [`output.txt`](output.txt), every verdict in [`data/judge.json`](data/judge.json).

## Where it breaks

A judge that's been calibrated is still a judge after the fact, over a batch. Production needs the referee on the field, scoring each run as it lands, and needs the failures it finds to become cases. Next: [20 · online evals and curated cases](../20-online-evals/).
