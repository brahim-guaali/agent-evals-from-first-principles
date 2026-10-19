"""Redaction of customer data in spans (stage 11): a collector processor.

Runs on every span before any exporter sees it, so the viewer, the archive and any backend
(Phoenix, Langfuse) only ever receive the redacted copy. What it does:

- emails, phone numbers, card numbers (13-19 digits that pass the Luhn check) and IP
  addresses inside any string become `[email]`, `[phone]`, `[card ****1234]`, `[ip]`;
- customer ids (`C-1042`, `C-7740-0012`) become a pseudonym (`cust_3f9a1c2b`): a keyed hash,
  so every span of one customer still carries the same value and traces can be grouped,
  but the id can't be read back without the key;
- it walks every attribute, event attribute, link attribute and the status message,
  including the JSON inside `gen_ai.input.messages` and tool arguments.

What it can't do: a regex doesn't recognise a name or a street address in free text
("deliver to Sara at Marina Gate 2"). Those need a model-based detector, or not capturing
message bodies at all, which is why the GenAI conventions make content capture opt-in.
"""
import hashlib
import hmac
import json
import os
import re

KEY = os.environ.get("REDACTION_KEY", "demo-key-change-me").encode()

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\w)(?:\+|00)\d{1,3}[\s-]?\(?\d{1,4}\)?(?:[\s-]?\d{2,4}){2,4}(?!\w)|(?<!\w)0\d{1,2}[\s-]?\d{3}[\s-]?\d{4}(?!\w)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
IP = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
CUSTOMER = re.compile(r"\bC-\d{4}(?:-\d{4})?\b")


def luhn(digits):
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d) * (2 if alt else 1)
        total += n - 9 if n > 9 else n
        alt = not alt
    return total % 10 == 0


def pseudonym(customer):
    return "cust_" + hmac.new(KEY, customer.encode(), hashlib.sha256).hexdigest()[:8]


def _card(m):
    digits = re.sub(r"\D", "", m.group(0))
    return f"[card ****{digits[-4:]}]" if luhn(digits) else m.group(0)


def scrub(text, counts=None):
    """Redact one string; counts (a dict) collects what was found."""
    def sub(pattern, repl, s, label):
        out, n = pattern.subn(repl, s)
        if counts is not None and n:
            counts[label] = counts.get(label, 0) + n
        return out
    text = sub(EMAIL, "[email]", text, "email")
    before = text
    text = CARD.sub(_card, text)
    if counts is not None and text != before:
        counts["card"] = counts.get("card", 0) + text.count("[card ")
    text = sub(PHONE, "[phone]", text, "phone")
    text = sub(IP, "[ip]", text, "ip")
    text = sub(CUSTOMER, lambda m: pseudonym(m.group(0)), text, "customer_id")
    return text


def _walk(v, counts):
    if isinstance(v, str):
        stripped = v.strip()
        if stripped[:1] in "[{":                       # JSON inside an attribute (messages, tool args)
            try:
                return json.dumps(_walk(json.loads(v), counts), ensure_ascii=False)
            except ValueError:
                pass
        return scrub(v, counts)
    if isinstance(v, list):
        return [_walk(x, counts) for x in v]
    if isinstance(v, dict):
        return {k: _walk(x, counts) for k, x in v.items()}
    return v


# Infrastructure fields, never customer data: scrubbing them only hides where a call went.
# (The first version had no such list and "redacted" 127.0.0.1 out of every HTTP span.)
KEEP = {"server.address", "server.port", "url.template", "http.request.method", "http.response.status_code",
        "gen_ai.request.model", "gen_ai.response.model", "gen_ai.response.id", "gen_ai.provider.name"}


def redact_span(span, counts=None):
    counts = {} if counts is None else counts
    span = dict(span)
    span["attributes"] = {k: v if k in KEEP else _walk(v, counts) for k, v in span.get("attributes", {}).items()}
    span["events"] = [{**e, "attributes": _walk(e.get("attributes", {}), counts)} for e in span.get("events", [])]
    span["links"] = [{**link, "attributes": _walk(link.get("attributes", {}), counts)} for link in span.get("links", [])]
    span["status_message"] = scrub(span.get("status_message") or "", counts)
    span["name"] = scrub(span["name"], counts)
    return span


def redact(spans):
    """The collector processor: every span, every exporter after it sees only this."""
    from services import collector
    out = []
    for s in spans:
        counts = {}
        out.append(redact_span(s, counts))
        for k, n in counts.items():
            collector.stats[f"redacted.{k}"] += n
    return out
