"""Per-edge trust tracking and tier selection (Spec sec. 3).

Trust is *not* shared state: each node keeps its own local estimate of how
trustworthy every neighbor has been, based purely on its own interaction
history with that neighbor. Two ends of the same edge may therefore see
slightly different trust values -- this is a deliberate and realistic
consequence of having no shared memory between processes.

The player's global trust threshold is broadcast (via the control plane)
identically to every node, but each node still applies it against its own
local per-neighbor trust score to decide, independently, which tier to use
for a given neighbor.
"""
from __future__ import annotations


class TrustTable:
    def __init__(self, neighbors: list[str], initial: float = 0.1):
        self._trust: dict[str, float] = {n: initial for n in neighbors}
        self.threshold: float = 0.5

    def bump(self, neighbor: str, amount: float) -> bool:
        """Adjust the score; returns True if the neighbor switched tier."""
        before = self.uses_high_trust_tier(neighbor)
        cur = self._trust.get(neighbor, 0.0)
        self._trust[neighbor] = min(1.0, max(0.0, cur + amount))
        return self.uses_high_trust_tier(neighbor) != before

    def decay(self, neighbor: str, amount: float) -> bool:
        return self.bump(neighbor, -amount)

    def score(self, neighbor: str) -> float:
        return self._trust.get(neighbor, 0.0)

    def uses_high_trust_tier(self, neighbor: str) -> bool:
        """Whether this node should currently use the TCP high-trust tier
        for the given neighbor, per the player-set global threshold."""
        return self.score(neighbor) >= self.threshold

    def set_threshold(self, value: float) -> list[str]:
        """Returns the neighbors whose tier flipped because of the change."""
        before = {n: self.uses_high_trust_tier(n) for n in self._trust}
        self.threshold = min(1.0, max(0.0, value))
        return [n for n, was in before.items() if self.uses_high_trust_tier(n) != was]

    def tier(self, neighbor: str) -> str:
        return "tcp" if self.uses_high_trust_tier(neighbor) else "udp"

    def as_dict(self) -> dict:
        return {"threshold": self.threshold, "scores": dict(self._trust)}
