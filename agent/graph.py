"""The refund agent: a LangGraph graph, started in the graph-engineering course.

classify -> (order, policy, fraud, history in parallel) -> decide -> verify -> manager approval
if the amount is over $500 or the customer had 3+ refunds in 30 days -> refund (with an
idempotency key) -> reply, which leaves a note in the customer's history for the next run.
Every message passes an input guardrail (prompt injection), and the streamed reply an
output check (amount, currency) with a correction if it fails (stage 06).
Lookups retry transient errors and validate what they get; a lookup that still fails is
recorded in `failures` and the run escalates with a reply that says what went wrong
(stage 05). The refund reply is streamed to the customer's chat as it's written (stage 04).
A follow-up in a conversation ("why only $72?") goes to `explain`, which answers from the
session and the notes, or hands the customer to a person. Every run belongs to a session
(stage 02): the customer's message and the agent's reply are stored as turns.
Two sub-agents (stage 03): a grey-zone fraud score (0.4 to 0.7) is handed to `fraud_review`,
and `explain` asks `policy_expert` before answering. Each hand-off is recorded in `handoffs`.
Messages that aren't refund requests go to `support`, a small agent loop inside one node.
State is checkpointed in SQLite, so a run can pause for the manager and resume in another
process.
"""
import operator
import sqlite3
import time
from pathlib import Path
from typing import Annotated, Optional, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from agentkit import judgments
from agentkit.trace import NullTracer
from agent import guardrails as gr
from agent import lookups as lk
from agent.subagents import FRAUD_REVIEW, POLICY_EXPERT
from agent.versions import run_meta
from agentkit import telemetry
from agentkit.llm import BudgetExceeded, ModelUnavailable
from agent.support import support_agent
from agent.verify import check

APPROVAL_LIMIT = 500
LOOKUPS = ["order", "policy", "fraud", "history"]
FREQUENT_CLAIMS = 3          # §8: refunds in the last 30 days that send any refund to a manager
MAX_ATTEMPTS = 3
DB = Path(__file__).resolve().parents[1] / "var" / "langgraph.db"


class RefundState(TypedDict, total=False):
    request: dict
    meta: dict                                  # prompt set, agent commit, environment (stage 08)
    trace_carrier: dict                         # traceparent of the run that paused (stage 09)
    budget_stop: bool                           # the meter refused a call (stage 12)
    trace_root: dict                            # traceparent of the run's root span, for replays (stage 15)
    prompt_versions: dict                       # prompt name -> version for this run (stage 16)
    experiment: dict                            # {"name", "arm"} if the run is in an A/B test (stage 16)
    session: dict
    budget: Optional[float]
    claim: dict
    order: dict
    policy: dict
    fraud: dict
    history: dict
    fraud_review: dict
    handoffs: Annotated[list, operator.add]     # every call to a sub-agent, in order
    failures: Annotated[list, operator.add]     # lookups that failed after retries (stage 05)
    guardrails: Annotated[list, operator.add]   # input and output checks, pass or flagged (stage 06)
    evidence: Annotated[list, operator.add]     # three parallel writers: append
    spent: Annotated[float, operator.add]       # every model call adds its cost
    attempts: Annotated[int, operator.add]
    decision: dict
    problems: list
    approval_request: dict
    approval: str
    payment: dict
    reply: dict


def build(llm, platform, tracer=NullTracer(), db=DB):
    def traced(name, fn):
        """Report each node to the live viewer, and give it a span under the run's root span
        (stage 09). LangGraph runs nodes on worker threads, so the root's context is passed
        explicitly rather than trusted to follow."""
        def node(state, config):
            step = config.get("metadata", {}).get("langgraph_step")
            tracer.node_started(name, round=step)
            root = TracedGraph.roots.get(config["configurable"]["thread_id"])
            with telemetry.span(f"node {name}", attributes={"langgraph.node": name, "langgraph.step": step},
                                context=root) as span:
                try:
                    update = fn(state, config)
                except Exception as e:
                    if type(e).__name__ == "GraphInterrupt":
                        tracer.node_waiting(name, "waiting for the operations manager")
                        span.set_attribute("refund_agent.paused", True)
                        telemetry.event("waiting_for_person", {"approval": "operations manager"}, span)
                    else:
                        tracer.node_failed(name, e)
                    raise
            tracer.node_finished(name, update)
            return update
        return node

    def charged(call):
        before = llm.usage.cost
        out = call()
        return out, llm.usage.cost - before

    def run_id(config):
        return config["configurable"]["thread_id"]

    def session_of(s, config):
        return s["request"].get("session_id") or run_id(config)

    def say(s, config, text):
        """The agent's side of the conversation: a stored turn and a message to the customer."""
        r = s["request"]
        platform.add_turn(session_of(s, config), r["customer"], "agent", text, run_id(config))
        return platform.notify(r["customer"], text) | {"text": text}

    def session(s, config):
        r = s["request"]
        before = platform.get_session(session_of(s, config))
        platform.add_turn(session_of(s, config), r["customer"], "customer", r["message"], run_id(config))
        return {"session": before, "evidence": [f"session: {len(before['turns'])} earlier turns"],
                "meta": s.get("meta") or run_meta()}

    def guard(s, config):
        before = llm.usage.cost
        with telemetry.span("guardrail prompt_injection", attributes={"guardrail.stage": "input"}) as span:
            events, used_model = gr.check_input(llm, s["request"]["message"])
            telemetry.set_attributes(span, {"guardrail.verdict": events[0]["verdict"], "guardrail.by": events[0]["by"],
                                            "guardrail.evidence": [str(x) for x in events[0]["evidence"]]})
        flagged = [e for e in events if e["verdict"] == "flagged"]
        if flagged:
            tracer.log(f"guardrail: prompt injection flagged by {flagged[0]['by']}: {flagged[0]['evidence']}")
        return {"guardrails": events, "spent": llm.usage.cost - before}

    def classify(s, config):
        claim, cost = charged(lambda: judgments.classify(llm, s["request"]["message"], s["session"]["turns"]))
        if claim.order_id:                           # customers type "a-9310" too (stage 07)
            claim.order_id = claim.order_id.strip().upper()
        return {"claim": claim.model_dump(), "spent": cost}

    def explain(s, config):
        r = s["request"]
        notes = platform.get_history(r["customer"])["notes"]
        handoff, cost1 = charged(lambda: POLICY_EXPERT.run(
            llm, platform, f"A customer asks: {r['message']!r}. Earlier in the conversation: "
            + " | ".join(f"{t['role']}: {t['text']}" for t in s["session"]["turns"][-4:]), "explain", tracer))
        e, cost = charged(lambda: judgments.explain(llm, r["message"], s["session"]["turns"], notes,
                                                    platform.get_policy(), handoff["result"]))
        cost += cost1
        reply = say(s, config, e.answer) | {"explanation": e.model_dump()}
        if e.wants_person:
            reply["ticket"] = platform.create_ticket(r["customer"], "other", "normal",
                                                     f"Disputes an earlier decision: {r['message'][:120]}")
        return {"reply": reply, "spent": cost, "handoffs": [handoff]}

    SEMANTIC = {"orders": ("execute_tool", "get_order"), "policy": ("execute_tool", "get_policy"),
                "fraud": ("execute_tool", "get_fraud_score"), "history": ("retrieval", "customer_history")}

    def looked_up(field, service, call, schema, describe):
        """A lookup node: fetch with retries and a schema check; a failure is data, not a crash.
        Its span says what it is in GenAI terms: a tool call, or a retrieval from memory."""
        op, what = SEMANTIC[service]
        attrs = {"gen_ai.operation.name": op, "gen_ai.conversation.id": telemetry.conversation_id(),
                 **({"gen_ai.tool.name": what, "gen_ai.tool.type": "function"} if op == "execute_tool"
                    else {"gen_ai.data_source.id": what})}
        kind = telemetry.SpanKind.CLIENT if op == "retrieval" else telemetry.SpanKind.INTERNAL
        with telemetry.span(f"{op} {what}", kind, attrs) as span:
            t0 = time.monotonic()
            value, failure, attempts = lk.fetch(service, call, schema, tracer)
            telemetry.record_tool(time.monotonic() - t0, what, failure["kind"] if failure else None)
            span.set_attribute("refund_agent.attempts", attempts)
            if failure:
                telemetry.fail(span, failure["kind"], failure["detail"])
                return {"failures": [failure], "evidence": [f"{service}: FAILED ({failure['kind']}, "
                                                            f"{failure['attempts']} attempts)"]}
        retried = f" after {attempts} attempts" if attempts > 1 else ""
        return {field: value, "evidence": [describe(value) + retried]}

    def order(s, config):
        return looked_up("order", "orders", lambda: platform.get_order(s["claim"]["order_id"]), lk.Order,
                         lambda o: f"order {o['order_id']}: ${o['total']}, {o['minutes_late']} min late")

    def policy(s, config):
        return looked_up("policy", "policy", platform.get_policy, lk.Policy, lambda p: f"policy {p['version']}")

    def fraud(s, config):
        return looked_up("fraud", "fraud", lambda: platform.get_fraud(s["request"]["customer"]), lk.Fraud,
                         lambda f: f"fraud score {f['score']}")

    def history(s, config):
        return looked_up("history", "history", lambda: platform.get_history(s["request"]["customer"]),
                         lk.History, lambda h: f"history: {h['refunds_30d']} refunds in 30 days, "
                                               f"{len(h['notes'])} notes")

    def screen(s, config):
        return {}                                   # code only: the route below decides

    def fraud_review(s, config):
        r, f = s["request"], s["fraud"]
        task = (f"Customer {r['customer']} asks for a refund on order {s['claim']['order_id']}. "
                f"Fraud score {f['score']}, signals: {', '.join(f['signals'])}. Review and give a verdict.")
        handoff, cost = charged(lambda: FRAUD_REVIEW.run(llm, platform, task, "refund_agent", tracer))
        return {"fraud_review": handoff["result"] or {"risk": "high", "evidence": ["review did not finish"]},
                "handoffs": [handoff], "spent": cost}

    def decide(s, config):
        feedback = "; ".join(s.get("problems") or []) or None
        claim = judgments.Classification(**s["claim"])
        try:
            d, cost = charged(lambda: judgments.decide(llm, s["request"]["message"], claim, s["order"],
                                                       s["policy"], s["fraud"], feedback,
                                                       s.get("history"), s.get("fraud_review")))
        except ValidationError as e:                 # the model's JSON didn't fit the schema
            return {"decision": None, "attempts": 1,
                    "problems": [f"your answer was not valid JSON for the schema: {str(e).splitlines()[0]}"]}
        return {"decision": d.model_dump(), "spent": cost, "attempts": 1, "problems": []}

    def verify(s, config):
        if s.get("decision") is None:
            return {"problems": s.get("problems") or ["no decision"]}
        return {"problems": check(judgments.Decision(**s["decision"]), s["order"], s["policy"], s["fraud"],
                                  s.get("history"), s.get("fraud_review"))}

    def ask_manager(s, config):
        d = s["decision"]
        return {"approval_request": platform.request_approval(
            config["configurable"]["thread_id"], s["order"]["order_id"], d["amount"], d["reason"]),
            "trace_carrier": telemetry.carrier()}        # the resumed run links back to this span

    def approval(s, config):
        answer = interrupt({"approval_id": s["approval_request"]["id"],
                            "question": f"Refund ${s['decision']['amount']:g}? ({s['decision']['reason']})"})
        return {"approval": answer}

    def refund(s, config):
        # One refund per order (stage 07): two runs for the same order at the same moment both
        # read a history with no refund in it; a key per run would let both pay.
        key = f"{s['order']['order_id']}:refund"
        return {"payment": platform.pay(s["order"]["order_id"], s["decision"]["amount"], idempotency_key=key)}

    def stream_reply(s, config):
        """Stage 04: write the reply while the customer watches it appear."""
        sid, seq = session_of(s, config), iter(range(10_000))
        before = llm.usage.cost
        system = None
        if "compose" in (s.get("prompt_versions") or {}):     # an experiment's arm pins its version (stage 16)
            from agent.experiments import text
            system = text("compose", s["prompt_versions"])
        out = judgments.compose(llm, s["request"]["message"], s["decision"],
                                lambda piece: platform.push_chunk(sid, run_id(config), next(seq), piece), system,
                                judgments.faq_for(s["decision"], s["policy"].get("faq")))
        return out, llm.usage.cost - before

    def reply(s, config):
        d, spent, streamed = s["decision"], 0.0, None
        if s.get("approval") == "rejected":
            text = "We reviewed your request and can't refund this order. A colleague will follow up."
        elif s.get("payment", {}).get("replayed"):
            text = ("This order has already been refunded, so there's nothing more to send. "
                    "The refund reaches your card in 5 to 10 working days.")
        else:
            streamed, spent = stream_reply(s, config)
            text = streamed["text"] or d["customer_message"]
        note = f"{d['action']} ${d['amount']:g} under {d['clause']}: {d['reason']}"
        if s.get("approval") == "rejected":
            note = f"manager rejected a ${d['amount']:g} refund"
        if s.get("payment", {}).get("replayed"):
            note = f"duplicate request: order already refunded (${d['amount']:g}), no second payment"
        if streamed and streamed["aborted"]:
            note += " (the customer left before the reply finished)"
        if any(e["verdict"] == "flagged" for e in s.get("guardrails", [])):
            note += " (prompt injection attempt flagged)"
        platform.add_note(s["request"]["customer"], run_id(config), s["order"]["order_id"], note)
        out = say(s, config, text)
        checks = []
        if streamed:
            out["stream"] = {k: streamed[k] for k in ("ttft", "seconds", "chunks", "aborted", "model")}
            with telemetry.span("guardrail output_facts", attributes={"guardrail.stage": "output"}) as span:
                checks = gr.check_output(text, d)          # after sending: a stream can't be unsent
                flags = [c for c in checks if c["verdict"] == "flagged"]
                telemetry.set_attributes(span, {"guardrail.verdict": "flagged" if flags else "pass",
                                                "guardrail.checks": [c["check"] for c in flags],
                                                "guardrail.evidence": [str(e) for c in flags for e in c["evidence"]]})
            if any(c["verdict"] == "flagged" for c in checks):
                out["correction"] = say(s, config, gr.correction(d))["text"]
                tracer.log(f"guardrail: reply corrected: {[c['evidence'] for c in checks]}")
        return {"reply": out, "spent": spent, "guardrails": checks}

    def support(s, config):
        # The hybrid: open-ended work, so a small agent loop runs *inside* this one node.
        out = support_agent(llm, platform, s["request"], tracer)
        platform.add_turn(session_of(s, config), s["request"]["customer"], "agent", out["text"], run_id(config))
        return {"reply": out}

    def escalate(s, config):
        failures = s.get("failures") or []
        if failures:
            text = lk.failure_message(failures, s.get("claim", {}).get("order_id"))
            why = [f"{f['service']}: {f['kind']} after {f['attempts']} attempts" for f in failures]
        else:
            text = "Thanks for your patience. A colleague will look at this and get back to you today."
            why = s.get("problems") or "over budget"
        return {"reply": say(s, config, text) | {"queue": "support-escalations", "why": why}}

    g = StateGraph(RefundState)
    for name, fn in [("session", session), ("guard", guard), ("classify", classify), ("explain", explain), ("screen", screen),
                     ("fraud_review", fraud_review), ("decide", decide), ("verify", verify),
                     ("ask_manager", ask_manager), ("approval", approval), ("refund", refund),
                     ("reply", reply), ("support", support), ("escalate", escalate)]:
        g.add_node(name, traced(name, fn))
    for name, fn in [("order", order), ("policy", policy), ("fraud", fraud), ("history", history)]:
        g.add_node(name, traced(name, fn))           # retries live in lookups.fetch (stage 05)

    g.add_edge(START, "session")
    g.add_edge("session", "guard")
    g.add_edge("guard", "classify")
    g.add_conditional_edges("classify", route_after_classify, LOOKUPS + ["explain", "support"])
    g.add_edge(LOOKUPS, "screen")
    g.add_conditional_edges("screen", route_after_lookups, ["escalate", "fraud_review", "decide"])
    g.add_edge("fraud_review", "decide")
    g.add_edge("decide", "verify")
    g.add_conditional_edges("verify", route_after_verify,
                            ["decide", "escalate", "ask_manager", "refund", "reply"])
    g.add_edge("ask_manager", "approval")
    g.add_conditional_edges("approval", lambda s: "refund" if s["approval"] == "approved" else "reply",
                            ["refund", "reply"])
    g.add_edge("refund", "reply")
    for done in ("reply", "support", "escalate", "explain"):
        g.add_edge(done, END)

    db.parent.mkdir(parents=True, exist_ok=True)
    traced_graph = TracedGraph(g.compile(checkpointer=SqliteSaver(sqlite3.connect(db, check_same_thread=False))))
    traced_graph.platform = platform
    return traced_graph


class TracedGraph:
    platform = None                                # set by build(), for the budget stop
    """The compiled graph, with one trace per invoke (stage 09).

    The root span `invoke_workflow refund_agent` carries the run id, the customer, the session
    (as `gen_ai.conversation.id`, also put in baggage so every span below can read it) and the
    versions from stage 08. A run that resumes after the manager's answer, perhaps in another
    process days later, starts a new trace linked to the span that paused: one trace per
    piece of work, joined by a link rather than one trace that lasts three days."""

    roots: dict = {}                               # thread id -> the root span's context

    def __init__(self, compiled):
        self.compiled = compiled

    def __getattr__(self, name):                   # get_state, update_state, get_graph, ...
        return getattr(self.compiled, name)

    def _budget_stop(self, config, request, session, thread, why):
        """End the run safely, without the model: what's done is done (a payment already made
        stays made, idempotently), and the customer is told a person will finish it."""
        state = dict(self.compiled.get_state(config).values)
        paid = state.get("payment")
        text = (f"Your refund of ${paid['amount']:.2f} is on its way." if paid and not paid.get("replayed") else
                "Thanks for your patience. A colleague will look at this and get back to you today.")
        if self.platform is not None and request.get("customer"):
            self.platform.add_turn(session, request["customer"], "agent", text, thread)
            self.platform.notify(request["customer"], text)
        telemetry.event("budget_stop", {"reason": why[:200]})
        return {**state, "budget_stop": True,
                "reply": {"text": text, "queue": "support-escalations", "why": "run budget exceeded"}}

    def replay(self, config, before, patch=None, as_node=None, why="replayed from checkpoint"):
        """Debugging (stage 15): run again from the last checkpoint before node `before`, with
        today's code and an optional patch to the state, as a new trace linked to the original.
        Side effects stay safe: the refund's idempotency key is per order, so a replay that
        reaches the payment gets `replayed` back instead of paying twice."""
        target = next((h for h in self.compiled.get_state_history(config) if before in (h.next or ())), None)
        if target is None:
            raise ValueError(f"no checkpoint before {before} for {config['configurable']['thread_id']}")
        fork = self.compiled.update_state(target.config, patch, as_node=as_node) if patch else target.config
        original = target.values.get("trace_root") or {}
        return self.invoke(None, fork, _replay=(original, f"{why}: from before {before}"))

    def invoke(self, value, config, **kw):
        from opentelemetry import baggage
        from opentelemetry import context as otel_context
        replay = kw.pop("_replay", None)
        thread = config["configurable"]["thread_id"]
        resuming = not isinstance(value, dict)
        request = (self.compiled.get_state(config).values.get("request") if resuming else value.get("request")) or {}
        session = request.get("session_id") or thread
        if replay:
            links = telemetry.resume_link(replay[0], replay[1])
        else:
            links = telemetry.resume_link(self.compiled.get_state(config).values.get("trace_carrier") or {}) if resuming else []
        meta = (value.get("meta") if not resuming else None) or {}
        attrs = {"gen_ai.operation.name": "invoke_workflow", "gen_ai.workflow.name": "refund_agent",
                 "gen_ai.conversation.id": session, "refund_agent.run_id": thread, "refund_agent.resumed": resuming,
                 "refund_agent.customer": request.get("customer"), "refund_agent.prompt_set": meta.get("prompt_set"),
                 "refund_agent.agent_version": meta.get("agent")}
        if not resuming:                           # an A/B arm and its prompt version (stage 16)
            exp, versions = value.get("experiment") or {}, value.get("prompt_versions") or {}
            attrs |= {"refund_agent.experiment": exp.get("name"), "refund_agent.arm": exp.get("arm"),
                      "gen_ai.prompt.name": "compose" if versions else None, "gen_ai.prompt.version": versions.get("compose")}
        token = otel_context.attach(baggage.set_baggage("gen_ai.conversation.id", session))
        try:
            name = "invoke_workflow refund_agent" + (" (replay)" if replay else " (resumed)" if resuming else "")
            with telemetry.span(name, attributes={**attrs, "refund_agent.replay": bool(replay)}, links=links) as root:
                t0 = time.monotonic()
                TracedGraph.roots[thread] = otel_context.get_current()
                if not resuming:
                    value = {**value, "trace_root": telemetry.carrier()}
                try:
                    state = self.compiled.invoke(value, config, **kw)
                except BudgetExceeded as e:             # the meter stopped the run (stage 12)
                    state = self._budget_stop(config, request, session, thread, str(e))
                    telemetry.fail(root, "budget_exceeded", str(e))
                except ModelUnavailable as e:           # every model out of quota: stop safely, don't crash
                    state = self._budget_stop(config, request, session, thread, str(e))
                    state["reply"]["why"] = "no model available"
                    telemetry.fail(root, "model_unavailable", str(e))
                finally:
                    TracedGraph.roots.pop(thread, None)
                paused = bool(self.compiled.get_state(config).next) and not state.get("budget_stop")
                d = state.get("decision") or {}
                telemetry.set_attributes(root, {
                    "refund_agent.paused": paused, "refund_agent.action": d.get("action"),
                    "refund_agent.amount": d.get("amount"), "refund_agent.clause": d.get("clause"),
                    "refund_agent.failures": len(state.get("failures") or []),
                    "refund_agent.budget_stop": bool(state.get("budget_stop")),
                    "refund_agent.guardrail_flags": sum(e["verdict"] == "flagged" for e in state.get("guardrails") or [])})
                if state.get("failures"):
                    telemetry.fail(root, "lookup_failed", "; ".join(f"{f['service']} {f['kind']}" for f in state["failures"]))
                telemetry.record_run(time.monotonic() - t0, "paused" if paused else d.get("action") or
                                     ("handoff" if (state.get("reply") or {}).get("ticket") else None),
                                     bool(state.get("failures")),
                                     any(e["verdict"] == "flagged" for e in state.get("guardrails") or []))
                return state
        finally:
            otel_context.detach(token)


def route_after_lookups(s):
    if s.get("failures"):
        return "escalate"                 # never decide on missing facts (stage 05)
    return "fraud_review" if grey_zone(s["fraud"]) else "decide"


def grey_zone(fraud):
    """§9: neither clearly fine nor clearly fraud, so the fraud-review sub-agent decides."""
    return 0.4 <= fraud["score"] <= 0.7


def route_after_classify(s):
    kind = s["claim"]["kind"]
    if kind == "follow_up" and s["session"]["turns"]:
        return "explain"
    if kind == "refund_request" and s["claim"].get("order_id"):
        return LOOKUPS
    return "support"


def route_after_verify(s):
    if s.get("budget") is not None and s.get("spent", 0) > s["budget"]:
        return "escalate"
    if s.get("problems"):
        return "decide" if s.get("attempts", 0) < MAX_ATTEMPTS else "escalate"
    d = s["decision"]
    if d["action"] != "refund":
        return "reply"
    return "ask_manager" if needs_manager(s) else "refund"


def needs_manager(s):
    """§3 and §8 are code: the amount, or a customer who claims often."""
    frequent = s.get("history", {}).get("refunds_30d", 0) >= FREQUENT_CLAIMS
    return s["decision"]["amount"] > APPROVAL_LIMIT or frequent


def spec(graph):
    """Nodes and edges for the live viewer, read from LangGraph's own description of the graph."""
    drawable = graph.get_graph()
    nodes = [n for n in drawable.nodes if not n.startswith("__")]
    kinds = {"session": "http · memory", "guard": "guardrail · rules, model if unsure", "explain": "model · calls policy_expert", "classify": "model",
             "screen": "code", "fraud_review": "sub-agent", "decide": "model", "support": "model · loop inside",
             "approval": "person", "refund": "http · money", "order": "http", "policy": "http",
             "fraud": "http", "history": "http · memory", "ask_manager": "http", "reply": "http"}
    edges = [{"from": e.source, "to": e.target, "cond": e.conditional}
             for e in drawable.edges if not e.source.startswith("__") and not e.target.startswith("__")]
    return {"entry": "session", "nodes": [{"id": n, "kind": kinds.get(n, "code")} for n in nodes],
            "edges": edges}
