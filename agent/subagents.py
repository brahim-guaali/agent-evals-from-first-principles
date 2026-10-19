"""Sub-agents: specialists the main agent hands a piece of work to (stage 03).

A sub-agent is a small agent loop with its own name, instructions and tools. It gets a task
in plain words, calls its tools as it sees fit, and ends by calling `finish` with a typed
result, so the main graph gets data back, not prose. Every hand-off is recorded (who asked,
which sub-agent, the task, each tool call, the result) because in part 2 each one becomes an
`invoke_agent` span with the tool calls nested under it.
"""
import json
import time
from dataclasses import dataclass, field

from google.genai import types

from agentkit import telemetry


@dataclass
class SubAgent:
    name: str
    description: str
    system: str
    tools: dict                     # tool name -> (FunctionDeclaration, fn(platform, **args))
    finish: dict                    # JSON schema of the result
    max_steps: int = 6

    def declarations(self):
        done = types.FunctionDeclaration(name="finish", description="Return your result and stop.",
                                         parameters_json_schema=self.finish)
        return [d for d, _ in self.tools.values()] + [done]

    def run(self, llm, platform, task, caller, tracer=None):
        """Run the loop inside an `invoke_agent {name}` span (stage 09). Returns the hand-off
        record; its `result` is the finish() arguments."""
        attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": self.name,
                 "gen_ai.agent.description": self.description, "gen_ai.conversation.id": telemetry.conversation_id(),
                 "refund_agent.handoff.from": caller}
        with telemetry.span(f"invoke_agent {self.name}", attributes=attrs) as span:
            record = self._run(llm, platform, task, caller, tracer)
            telemetry.set_attributes(span, {"refund_agent.handoff.steps": len(record["steps"]),
                                            "refund_agent.handoff.result": record["result"]})
            if record["result"] is None:
                telemetry.fail(span, "step_limit", record.get("error", ""))
            return record

    def _run(self, llm, platform, task, caller, tracer):
        record = {"from": caller, "to": self.name, "task": task, "steps": [], "result": None}
        t0 = time.monotonic()
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=task)])]
        for step in range(1, self.max_steps + 1):
            response = llm.chat(self.system, contents, self.declarations())
            contents.append(response.candidates[0].content)
            calls = response.function_calls or []
            if not calls:                                   # answered in prose: ask for finish()
                contents.append(types.Content(role="user", parts=[types.Part.from_text(
                    text="Call finish with your result.")]))
                continue
            parts = []
            for call in calls:
                args = dict(call.args or {})
                if call.name == "finish":
                    record.update(result=args, seconds=round(time.monotonic() - t0, 2))
                    if tracer:
                        tracer.log(f"{self.name} → {caller}: {json.dumps(args, ensure_ascii=False)[:160]}")
                    return record
                decl, fn = self.tools[call.name]
                with telemetry.span(f"execute_tool {call.name}", attributes={
                        "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": call.name,
                        "gen_ai.tool.type": "function", "gen_ai.tool.call.id": getattr(call, "id", None),
                        "gen_ai.agent.name": self.name, "gen_ai.tool.description": decl.description}) as tspan:
                    t0 = time.monotonic()
                    try:
                        result = fn(platform, **args)
                        telemetry.record_tool(time.monotonic() - t0, call.name)
                    except Exception as e:                  # a failing tool is information
                        result = {"error": f"{type(e).__name__}: {e}"}
                        telemetry.fail(tspan, type(e).__name__, str(e))
                        telemetry.record_tool(time.monotonic() - t0, call.name, type(e).__name__)
                    if telemetry.capture_content():
                        telemetry.set_attributes(tspan, {"gen_ai.tool.call.arguments": args,
                                                         "gen_ai.tool.call.result": result})
                record["steps"].append({"tool": call.name, "args": args,
                                        "result": json.dumps(result, ensure_ascii=False)[:300]})
                if tracer:
                    tracer.log(f"{self.name} step {step}: {call.name}({json.dumps(args, ensure_ascii=False)})")
                parts.append(types.Part.from_function_response(name=call.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))
        record.update(result=None, seconds=round(time.monotonic() - t0, 2), error="step limit reached")
        return record


def _decl(name, description, **props):
    required = [k for k, v in props.items() if not v.pop("optional", False)]
    return types.FunctionDeclaration(name=name, description=description, parameters_json_schema={
        "type": "object", "properties": props, "required": required})


FRAUD_REVIEW = SubAgent(
    name="fraud_review",
    description="Investigates a refund when the fraud score is in the grey zone (0.4 to 0.7).",
    system="""You are a fraud analyst at a food-delivery app. A refund request has a fraud score
between 0.4 and 0.7, so a person-like review decides. Look at the customer's devices and their
refund history, then decide: high risk (refuse) or low risk (go ahead). A device shared with an
account that has a fraud score above 0.7 is high risk. A device shared within one household is
not, on its own. Call finish with your verdict and the evidence it rests on.""",
    tools={
        "get_devices": (_decl("get_devices", "Devices the customer used and other accounts on them.",
                              customer={"type": "string"}),
                        lambda p, customer: p.get_devices(customer)),
        "get_history": (_decl("get_history", "The customer's past refunds and notes.",
                              customer={"type": "string"}),
                        lambda p, customer: p.get_history(customer)),
        "get_fraud_score": (_decl("get_fraud_score", "Fraud score and signals of any account.",
                                  customer={"type": "string"}),
                            lambda p, customer: p.get_fraud(customer)),
    },
    finish={"type": "object", "required": ["risk", "evidence"], "properties": {
        "risk": {"type": "string", "enum": ["low", "high"]},
        "evidence": {"type": "array", "items": {"type": "string"}}}},
)

POLICY_EXPERT = SubAgent(
    name="policy_expert",
    description="Answers a question about the refund policy, citing clauses and FAQ entries.",
    system="""You answer questions about a food-delivery app's refund policy. Search the policy
(clauses §1-§9 and FAQ entries F1-F5) before answering; search again with other words if the
first results don't cover the question. Answer only from what you found and list the ids you
used. If nothing you found covers the question, say so: never guess. Call finish.""",
    tools={
        "search_policy": (_decl("search_policy", "Search policy clauses and FAQ answers.",
                                query={"type": "string"}),
                          lambda p, query: p.search_policy(query)),
    },
    finish={"type": "object", "required": ["answer", "sources", "covered"], "properties": {
        "answer": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "like §1 or F2"},
        "covered": {"type": "boolean", "description": "false if the policy doesn't answer it"}}},
)
