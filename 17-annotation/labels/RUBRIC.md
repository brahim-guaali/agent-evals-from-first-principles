# How the 30 reference labels were made (2026-10-22)

No person labelled these runs. The labels come from two sources, kept apart so they can be reported apart.

## Factual questions: from the known right answers

| Question | Label | Source |
|---|---|---|
| `decision_correct` | yes when the run's outcome (action, amount, and a manager's approval when §3 or §8 requires it) equals the expected outcome of its case | every queued run is one of the 18 hand-written cases or a traffic variant of one ([`agentkit/cases.py`](../../agentkit/cases.py)); the run's outcome was checked against the case's `expect` when it was queued ([`../output.txt`](../output.txt), all 30 ✓) |
| `amount_currency_right` | yes when every amount in the reply is the decided one (or the $5 credit §5/§6 give) and in US dollars | the decision in the snapshot and the reply text; any other currency (riyal, ريال, AED, dirham, SAR) is a no |

These are reference labels: anyone checking the same records gets the same answer.

## Judgement questions: a second model, with this rubric

`reply_grounded` and `tone` were labelled by Claude (Anthropic), a different model from the judge (Gemini), in one pass over the 30 snapshots, before the judge ran. Same rule for every reply: "ready to use" and "right now" count as "now". Model-against-model agreement is weaker evidence than a person's labels; a person-labelled set is the next step.

**`reply_grounded`: does the reply say only what the facts, the policy and the FAQ support?** No when it states:
- an amount, reason or clause the decision doesn't have;
- a timeline that doesn't match the FAQ: refunds reach the card in 5 to 10 working days (F5); credit is added within an hour and lasts 90 days (F4). "3 to 5 business days" and "available right now" are both a no;
- a promise of future action nobody made ("we'll make sure this doesn't happen again").

Polite filler ("we hope to serve you better") is not a claim.

**`tone`: how does the reply read to an upset customer?**
- **good**: acknowledges the problem, gives the outcome plainly, says why in words the customer understands;
- **ok**: correct but cold or vague: a refusal with no reason a customer can follow, a clause number instead of an explanation, a form-letter sign-off;
- **bad**: rude, blaming, confusing or misleading.
