"""What produced a run (stage 08): the prompts, the agent's code, the models, the environment.

Every prompt gets a short hash of its text, and the whole set a hash of those, so two runs
with the same `prompt_set` ran the same instructions. Change one word in any prompt and
the hash changes: that's how a dashboard can be filtered by version, and how an A/B test
(stage 16) tells its arms apart. `agent` is the git commit of this repo.
"""
import hashlib
import os
import subprocess
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def prompts():
    from agent import guardrails, subagents, support
    from agentkit import judgments
    found = {name.lower(): getattr(judgments, name) for name in ("CLASSIFY", "DECIDE", "EXPLAIN", "COMPOSE")}
    found |= {"fraud_review": subagents.FRAUD_REVIEW.system, "policy_expert": subagents.POLICY_EXPERT.system,
              "support": support.SYSTEM, "injection_check": guardrails.CHECK}
    return found


@lru_cache(maxsize=1)
def _git():
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "agent", "agentkit"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (sha or "unknown") + ("-dirty" if dirty else "")
    except Exception:
        return "unknown"


def run_meta(environment=None, versions=None):
    """`versions` (prompt name -> version, stage 16) swaps in that version's text before hashing."""
    found = prompts()
    if versions:
        from agent.experiments import text
        found |= {name: text(name, versions) for name in versions}
    p = {name: _h(t) for name, t in found.items()}
    return {"agent": _git(), "prompt_set": _h("".join(f"{k}={v}" for k, v in sorted(p.items()))), "prompts": p,
            "environment": environment or os.environ.get("AGENT_ENV", "dev")}
