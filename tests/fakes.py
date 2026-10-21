"""A scripted stand-in for Gemini, so the tests run without a key or network."""
from types import SimpleNamespace

from google.genai import types

from agentkit.llm import Usage


class ScriptedChat:
    """Replays a list of turns. A turn is a list of (tool, args), or a string (final answer)."""

    def __init__(self, turns):
        self.turns, self.usage = list(turns), Usage()

    def chat(self, system, contents, tools):
        turn = self.turns.pop(0)
        self.usage.calls += 1
        self.usage.input_tokens += sum(len(str(c)) for c in contents) // 4
        if isinstance(turn, str):
            parts = [types.Part.from_text(text=turn)]
            calls = None
        else:
            parts = [types.Part.from_function_call(name=n, args=a) for n, a in turn]
            calls = [SimpleNamespace(name=n, args=a) for n, a in turn]
        content = types.Content(role="model", parts=parts)
        return SimpleNamespace(candidates=[SimpleNamespace(content=content)],
                               function_calls=calls, text=turn if isinstance(turn, str) else None)


def finish(**result):
    """A sub-agent's last turn: call finish() with this result."""
    return [("finish", result)]


DEFAULT_SUBAGENTS = {
    "fraud analyst": [finish(risk="low", evidence=["scripted"])],
    "refund policy": [finish(answer="The order total is the food items only.", sources=["F1"], covered=True)],
}


class ScriptedStructured:
    """Answers structured() calls with a function of (schema name, prompt), and runs sub-agent
    loops (chat) from scripts keyed by a phrase in the sub-agent's instructions."""

    def __init__(self, answer, subagents=None):
        self.answer, self.usage = answer, Usage()
        self.scripts = {k: list(v) for k, v in (subagents or DEFAULT_SUBAGENTS).items()}
        self.chats = {}

    REPLY = ["We're sorry ", "your order was late. ", "Your refund is on its way ", "and should arrive in 5 to 10 days."]

    def stream(self, system, prompt, on_chunk):
        """Streams REPLY piece by piece; stops when on_chunk returns False (the customer left)."""
        self.usage.calls += 1
        self.stream_systems = getattr(self, "stream_systems", []) + [system]
        self.stream_prompts = getattr(self, "stream_prompts", []) + [prompt]
        text, chunks = "", 0
        for piece in self.REPLY:
            text, chunks = text + piece, chunks + 1
            if on_chunk(piece) is False:
                return {"text": text, "ttft": 0.01, "seconds": 0.02, "chunks": chunks, "aborted": True,
                        "model": "scripted"}
        return {"text": text, "ttft": 0.01, "seconds": 0.02, "chunks": chunks, "aborted": False, "model": "scripted"}

    def chat(self, system, contents, tools):
        key = next(k for k in self.scripts if k in system)
        if key not in self.chats or len(contents) == 1:          # a new hand-off restarts its script
            self.chats[key] = ScriptedChat(self.scripts[key])
        response = self.chats[key].chat(system, contents, tools)
        self.usage.calls += 1
        return response

    def structured(self, system, prompt, schema):
        self.usage.calls += 1
        self.usage.input_tokens += len(prompt) // 4
        return schema.model_validate(self.answer(schema.__name__, prompt))
