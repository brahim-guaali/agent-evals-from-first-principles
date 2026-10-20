"""A thin Gemini client: tool calls for the loop, structured output for graph nodes.

It also does the dull, necessary parts: it stays under the free tier's rate limit, retries
on 429 and 503, falls back to a lighter model when the main one is overloaded, and counts
every call and token so stages can compare cost.
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field

from google import genai
from google.genai import errors, types
from opentelemetry.trace import SpanKind

from agentkit import telemetry

MAX_OUTPUT = int(os.environ.get("GEMINI_MAX_OUTPUT", "512"))   # an output cap on every call (stage 12)
MODELS = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",")
RPM = float(os.environ.get("GEMINI_RPM", "8"))

# Paid-tier list prices, USD per 1M tokens: input, output (thinking included), cached input.
# From ai.google.dev/gemini-api/docs/pricing, checked 2026-10-20 (stage 12). The free tier bills
# nothing; these say what the same traffic would cost on a paid key.
PRICES = {
    "gemini-3.5-flash-lite": (0.30, 2.50, 0.03),
    "gemini-3.1-flash-lite": (0.25, 1.50, 0.025),
    "gemini-3.8-flash": (0.75, 3.75, 0.075),
    "gemini-3.6-flash": (0.75, 3.75, 0.075),
}
DEFAULT_PRICE = PRICES["gemini-3.5-flash-lite"]


def price_of(model, input_tokens, output_tokens, cached_tokens=0):
    """USD for one call; cached input tokens are billed at the cached rate."""
    p_in, p_out, p_cache = PRICES.get(model, DEFAULT_PRICE)
    fresh = max(0, input_tokens - cached_tokens)
    return (fresh * p_in + cached_tokens * p_cache + output_tokens * p_out) / 1e6


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    models: dict = field(default_factory=dict)        # which model answered, and how often
    events: list = field(default_factory=list)        # 429s, server errors, fallbacks (stage 05)
    versions: set = field(default_factory=set)        # model versions Gemini reported (stage 08)
    cached_tokens: int = 0                            # input tokens served from Gemini's cache (stage 12)
    spent: float = 0.0                                # USD at list price, per model (stage 12)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, response, seconds, model=None):
        u = response.usage_metadata
        with self._lock:
            self.calls += 1
            if model:
                self.models[model] = self.models.get(model, 0) + 1
            if getattr(response, "model_version", None):
                self.versions.add(response.model_version)
            inp, out = u.prompt_token_count or 0, (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
            cached = u.cached_content_token_count or 0
            self.input_tokens += inp
            self.output_tokens += out
            self.cached_tokens += cached
            self.spent += price_of(model, inp, out, cached)
            self.seconds += seconds

    @property
    def cost(self):
        return self.spent

    def snapshot(self):
        return {"calls": self.calls, "models": dict(self.models), "cached_tokens": self.cached_tokens,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost": round(self.cost, 6)}

    def __str__(self):
        models = ", ".join(f"{n}× {m}" for m, n in self.models.items())
        return (f"{self.calls} model calls ({models}), {self.input_tokens:,} tokens in, "
                f"{self.output_tokens:,} out, ~${self.cost:.4f}")


class _RateLimiter:
    """At most RPM calls per minute across all threads."""

    def __init__(self, rpm):
        self.interval, self.next_at, self.lock = 60.0 / rpm, 0.0, threading.Lock()

    def wait(self):
        """Returns how long this call waited for its turn."""
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.interval
        time.sleep(max(0.0, at - now))
        return max(0.0, at - now)


_limiter = _RateLimiter(RPM)
_exhausted: set = set()          # models whose free daily quota ran out in this process


class _MidStream(Exception):
    """A stream that failed after words reached the customer: retrying would repeat them."""


def _messages(contents):
    """The prompt in the GenAI conventions' message shape (only when content capture is on)."""
    if isinstance(contents, str):
        return json.dumps([{"role": "user", "parts": [{"type": "text", "content": contents}]}], ensure_ascii=False)
    out = []
    for c in contents:
        parts = []
        for p in c.parts or []:
            if p.text:
                parts.append({"type": "text", "content": p.text})
            elif p.function_call:
                parts.append({"type": "tool_call", "name": p.function_call.name, "arguments": dict(p.function_call.args or {})})
            elif p.function_response:
                parts.append({"type": "tool_call_response", "response": str(p.function_response.response)[:2000]})
        out.append({"role": "assistant" if c.role == "model" else c.role, "parts": parts})
    return json.dumps(out, ensure_ascii=False, default=str)


def _output(response):
    try:
        return _messages([response.candidates[0].content])
    except (AttributeError, IndexError, TypeError):
        return "[]"


class BudgetExceeded(Exception):
    """The metering proxy refused a call: this run has spent its budget (stage 12)."""


class ModelUnavailable(Exception):
    """Every model is out of quota or failing after all retries. A provider's quota is a
    budget someone else set (stage 12): the run must stop safely, not crash."""


class Gemini:
    def __init__(self, models=None, usage=None, run_id=None, budget=None, customer=None):
        if not os.environ.get("GEMINI_API_KEY"):
            raise SystemExit("Set GEMINI_API_KEY (free: https://aistudio.google.com/apikey).")
        meter = os.environ.get("METER_URL")
        if meter:                                   # every call through the meter (stage 12)
            headers = {"x-run-id": run_id or "unknown", **({"x-run-budget": str(budget)} if budget else {}),
                       **({"x-customer": customer} if customer else {})}
            self.client = genai.Client(http_options=types.HttpOptions(base_url=f"{meter.rstrip('/')}/gemini",
                                                                      headers=headers))
        else:
            self.client = genai.Client()
        self.models = models or MODELS
        self.usage = usage or Usage()

    corrupt = {}              # fault injection (stage 05): {"Decision": 1} breaks the next Decision answer

    def _event(self, kind, model, attempt, **extra):
        with self.usage._lock:
            self.usage.events.append({"kind": kind, "model": model, "attempt": attempt, **extra})
        telemetry.event(f"gen_ai.{kind}", {"gen_ai.request.model": model, "attempt": attempt, **extra})

    def _model_for(self, attempt):
        """Two tries per model, in order, skipping models whose daily quota is spent."""
        usable = [m for m in self.models if m not in _exhausted] or self.models[-1:]
        return usable[min(attempt // 2, len(usable) - 1)]

    def _with_retries(self, work):
        """Run work(model) with rate limiting, retries on 429/5xx/dropped connections and model
        fallback. Every retry, quota hit and fallback is recorded in usage.events (stage 05)."""
        last, previous = None, None
        for attempt in range(6):
            model = self._model_for(attempt)
            if previous and model != previous:
                self._event("fallback", model, attempt + 1, after=previous)
            previous = model
            waited = _limiter.wait()
            if waited > 0.05:                              # our own client-side limit, not Gemini
                telemetry.event("rate_limiter.wait", {"seconds": round(waited, 2)})
                span = telemetry.current()              # with tracing off this span records nothing,
                so_far = (getattr(span, "attributes", None) or {}).get("rate_limiter.wait_s", 0)   # and has no attributes
                span.set_attribute("rate_limiter.wait_s", round(so_far + waited, 2))
            try:
                return work(model)
            except (errors.ServerError, errors.ClientError) as e:
                code = getattr(e, "code", None)
                if code == 402:                            # the meter's budget stop: never retry
                    self._event("budget_exceeded", model, attempt + 1)
                    raise BudgetExceeded(str(e)[:300]) from e
                if code not in (429, 500, 503) and isinstance(e, errors.ClientError):
                    raise
                last = e
                if code == 429 and "PerDay" in str(e):
                    _exhausted.add(model)                  # no point retrying until tomorrow
                    self._event("daily_quota_exhausted", model, attempt + 1)
                    continue
                wait = min(30, 2 ** attempt * 2)
                self._event("rate_limited" if code == 429 else f"server_error_{code}", model, attempt + 1, wait=wait)
                time.sleep(wait)
            except _MidStream:
                raise
            except Exception as e:                         # dropped connections
                last = e
                self._event("connection_error", model, attempt + 1, error=type(e).__name__)
                time.sleep(2 ** attempt)
        raise ModelUnavailable(f"no model answered after 6 attempts: {str(last)[:200]}")

    @staticmethod
    def _response_attributes(span, response, model):
        """GenAI conventions: what answered, why it stopped, and the tokens it used."""
        u = response.usage_metadata
        cand = (response.candidates or [None])[0]
        telemetry.set_attributes(span, {
            "gen_ai.response.model": getattr(response, "model_version", None) or model,
            "gen_ai.response.id": getattr(response, "response_id", None),
            "gen_ai.response.finish_reasons": [cand.finish_reason.name] if cand is not None and cand.finish_reason else None,
            "gen_ai.usage.input_tokens": u.prompt_token_count if u else None,
            "gen_ai.usage.output_tokens": ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else None,
            "gen_ai.usage.reasoning.output_tokens": (u.thoughts_token_count or None) if u else None,
            "gen_ai.usage.cache_read.input_tokens": (u.cached_content_token_count or None) if u else None,
            "refund_agent.model_used": model,
            # Not in the conventions; Langfuse reads it as the call's cost (stage 12).
            "gen_ai.usage.cost": round(price_of(model, (u.prompt_token_count or 0) if u else 0,
                                                ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
                                                (u.cached_content_token_count or 0) if u else 0), 8),
        })

    def _metrics(self, response, seconds, model, ttft=None):
        u = response.usage_metadata
        inp = (u.prompt_token_count or 0) if u else 0
        out = ((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0
        telemetry.record_inference(seconds, self.models[0], getattr(response, "model_version", None) or model, ttft=ttft,
                                   input_tokens=inp, output_tokens=out,
                                   cost=price_of(model, inp, out, (u.cached_content_token_count or 0) if u else 0))

    def _start(self, config, stream=False):
        """The CLIENT span of one model call: `chat {model}` (stage 09)."""
        attrs = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": telemetry.PROVIDER,
                 "gen_ai.request.model": self.models[0], "gen_ai.request.temperature": config.temperature,
                 "gen_ai.request.max_tokens": config.max_output_tokens,
                 "gen_ai.request.stream": stream or None, "gen_ai.conversation.id": telemetry.conversation_id(),
                 "server.address": "generativelanguage.googleapis.com"}
        if telemetry.capture_content():
            attrs["gen_ai.system_instructions"] = [{"type": "text", "content": str(config.system_instruction)}]
        return telemetry.span(f"chat {self.models[0]}", SpanKind.CLIENT, attrs)

    def _generate(self, contents, config):
        with self._start(config) as span:
            if telemetry.capture_content():
                span.set_attribute("gen_ai.input.messages", _messages(contents))

            def work(model):
                t0 = time.monotonic()
                response = self.client.models.generate_content(model=model, contents=contents, config=config)
                self.usage.add(response, time.monotonic() - t0, model)
                self._response_attributes(span, response, model)
                self._metrics(response, time.monotonic() - t0, model)
                if telemetry.capture_content():
                    span.set_attribute("gen_ai.output.messages", _output(response))
                return response
            return self._with_retries(work)

    def chat(self, system, contents, tools):
        """For the agent loop: the model sees the whole transcript and may call tools."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=tools)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0, max_output_tokens=MAX_OUTPUT,
        )
        return self._generate(contents, config)

    def stream(self, system, prompt, on_chunk):
        """Stream a text answer (stage 04). on_chunk(text) is called for every piece as it
        arrives; if it returns False the customer is gone, so we stop generating. Returns the
        text, time to first token, total time, number of chunks and whether it was cut off."""
        config = types.GenerateContentConfig(system_instruction=system, temperature=0.3, max_output_tokens=400)
        with self._start(config, stream=True) as span:
            return self._stream(span, config, prompt, on_chunk)

    def _stream(self, span, config, prompt, on_chunk):
        def work(model):
            t0, text, chunks, ttft, final = time.monotonic(), "", 0, None, None
            stream = self.client.models.generate_content_stream(model=model, contents=prompt, config=config)
            try:
                for response in stream:
                    final, piece = response, response.text or ""
                    if not piece:
                        continue
                    ttft = ttft if ttft is not None else time.monotonic() - t0
                    text, chunks = text + piece, chunks + 1
                    if on_chunk(piece) is False:
                        stream.close()                     # stop paying for words nobody reads
                        break
            except Exception as e:
                if chunks:                                 # failed mid-stream: words already reached the customer
                    raise _MidStream(e) from e
                raise
            aborted = final is None or not (final.candidates and final.candidates[0].finish_reason)
            if final is not None and final.usage_metadata:
                self.usage.add(final, time.monotonic() - t0, model)
                self._response_attributes(span, final, model)
                self._metrics(final, time.monotonic() - t0, model, ttft)
            telemetry.set_attributes(span, {"gen_ai.response.time_to_first_chunk": ttft, "refund_agent.chunks": chunks,
                                            "refund_agent.stream_aborted": aborted})
            if aborted:
                telemetry.event("stream.aborted", {"chunks_sent": chunks, "reason": "the customer closed the chat"})
            return {"text": text, "ttft": ttft, "seconds": time.monotonic() - t0, "chunks": chunks,
                    "aborted": aborted, "model": model}
        return self._with_retries(work)

    def structured(self, system, prompt, schema):
        """For a graph node: one narrow question in, one typed answer (a pydantic model) out."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0, max_output_tokens=MAX_OUTPUT,
        )
        response = self._generate(prompt, config)
        if self.corrupt.get(schema.__name__, 0) > 0:     # as if the model stopped mid-JSON
            self.corrupt[schema.__name__] -= 1
            self._event("invalid_output", "injected", 1)
            return schema.model_validate_json(response.text[: len(response.text) // 2])
        return response.parsed if response.parsed is not None else schema.model_validate_json(response.text)
