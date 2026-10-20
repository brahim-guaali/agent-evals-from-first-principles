"""Prompt versions and A/B experiments (stage 16).

A prompt is kept by name and version. A run carries the versions it uses (`prompt_versions`
in its state, set when it starts), so two runs side by side can use different prompts,
and every span and metric says which. An experiment splits customers between arms by a hash
of the customer id: the same customer always lands in the same arm (sticky), and the split
needs no shared state.
"""
import hashlib
from dataclasses import dataclass, field

from agentkit import judgments

COMPOSE_V2 = judgments.COMPOSE        # the default since part 4

PROMPTS = {
    "compose": {"v1": judgments.COMPOSE_V1, "v2": COMPOSE_V2},
}


def text(name, versions):
    """The prompt text for a run: its pinned version, or v2 (the default since part 4)."""
    return PROMPTS[name][(versions or {}).get(name, "v2")]


@dataclass
class Experiment:
    name: str
    arms: dict                       # arm -> {prompt name: version}
    split: dict = field(default_factory=dict)       # arm -> share; even if empty

    def arm_for(self, customer):
        h = int(hashlib.sha256(f"{self.name}:{customer}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        shares = self.split or {a: 1 / len(self.arms) for a in self.arms}
        acc = 0.0
        for arm, share in shares.items():
            acc += share
            if h <= acc:
                return arm
        return list(self.arms)[-1]

    def versions_for(self, customer):
        arm = self.arm_for(customer)
        return arm, dict(self.arms[arm])


COMPOSE_TEST = Experiment("compose-v2", {"A": {"compose": "v1"}, "B": {"compose": "v2"}})
