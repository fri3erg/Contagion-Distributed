"""Node lifecycle state machine (Spec sec. 2).

HEALTHY -> INCUBATING (covert) -> OVERT (zombie) -> CURED
(any state) -> DEAD  (snipe, or a mis-aimed cure)

This module holds *only* local state for a single node process. There is
no shared memory: every other node in the network only ever learns about
this state indirectly, through the two data-plane protocols (heartbeat
anomalies, cure-relay failures) or through this node's own observability
events -- never by reading this object directly.
"""
from __future__ import annotations

import time
from enum import Enum


class NodeState(str, Enum):
    HEALTHY = "HEALTHY"
    INCUBATING = "INCUBATING"
    OVERT = "OVERT"
    CURED = "CURED"
    DEAD = "DEAD"


TERMINAL_STATES = (NodeState.DEAD,)
INFECTED_STATES = (NodeState.INCUBATING, NodeState.OVERT)
COVERTLY_HEALTHY_ON_HEARTBEAT = (NodeState.HEALTHY, NodeState.INCUBATING, NodeState.CURED)


class StateMachine:
    """Mutable lifecycle state for the local node."""

    def __init__(self, rng, incubation_min: int, incubation_max: int):
        self._rng = rng
        self._incubation_min = incubation_min
        self._incubation_max = incubation_max

        self.state: NodeState = NodeState.HEALTHY
        self.incubation_end_tick: int | None = None
        self.state_changed_at: float = time.time()

    # -- queries -----------------------------------------------------
    @property
    def is_alive(self) -> bool:
        return self.state != NodeState.DEAD

    @property
    def is_infected(self) -> bool:
        return self.state in INFECTED_STATES

    @property
    def answers_heartbeat_normally(self) -> bool:
        # HEALTHY and INCUBATING are indistinguishable on this channel.
        return self.state in COVERTLY_HEALTHY_ON_HEARTBEAT

    @property
    def relay_success_probability_key(self) -> str:
        """Which relay-probability bucket this node falls into (Spec sec. 5)."""
        return "infected" if self.is_infected else "healthy"

    # -- transitions ---------------------------------------------------
    def infect(self, current_tick: int) -> bool:
        """Try to move HEALTHY -> INCUBATING. No-op if not currently HEALTHY."""
        if self.state != NodeState.HEALTHY:
            return False
        self.state = NodeState.INCUBATING
        duration = self._rng.randint(self._incubation_min, self._incubation_max)
        self.incubation_end_tick = current_tick + duration
        self.state_changed_at = time.time()
        return True

    def maybe_turn_overt(self, current_tick: int) -> bool:
        """Advance INCUBATING -> OVERT once the incubation timer elapses."""
        if self.state != NodeState.INCUBATING:
            return False
        if self.incubation_end_tick is None or current_tick < self.incubation_end_tick:
            return False
        self.state = NodeState.OVERT
        self.state_changed_at = time.time()
        return True

    def cure(self) -> tuple[bool, NodeState]:
        """Apply the player's cure action (Spec sec. 7).

        Returns (was_correct, new_state). Correct only if the target was
        INCUBATING/OVERT (becomes CURED); otherwise the node dies.
        """
        prev = self.state
        if prev in INFECTED_STATES:
            self.state = NodeState.CURED
            self.incubation_end_tick = None
            self.state_changed_at = time.time()
            return True, self.state
        if prev == NodeState.DEAD:
            return False, self.state  # already dead, no-op
        self.state = NodeState.DEAD
        self.state_changed_at = time.time()
        return False, self.state

    def snipe(self) -> tuple[bool, NodeState]:
        """Apply the player's snipe action (Spec sec. 7).

        Kills the target regardless. Returns (was_correct, new_state):
        correct if the target was INCUBATING/OVERT.
        """
        prev = self.state
        was_correct = prev in INFECTED_STATES
        self.state = NodeState.DEAD
        self.state_changed_at = time.time()
        return was_correct, self.state

    def as_dict(self) -> dict:
        return {
            "state": self.state.value,
            "incubation_end_tick": self.incubation_end_tick,
        }
