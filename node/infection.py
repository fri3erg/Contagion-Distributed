"""Infection transmission rolls (Spec sec. 4).

Crucial distributed-systems point: an infected node is the only party that
ever knows it is infected, so it must be the one to decide -- unilaterally,
locally -- whether a given contact transmits the pathogen. It does so by
attaching a hidden exposure marker to the outgoing message it was sending
anyway (a heartbeat or a trust-sync payload). The receiving node has no
special knowledge of the sender; it just interprets a field on a message it
received, exactly as it would any other protocol field. No shared memory,
no peeking at the neighbor's true state.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .state import StateMachine


def roll_exposure(state: "StateMachine", cfg: "Config", rng, tier: str) -> bool:
    """Called by the (possibly infected) sender before it ships a message.

    tier is "heartbeat" or "trust_sync". Returns True if this contact
    should carry the pathogen.
    """
    if state.state.value == "OVERT":
        if tier == "trust_sync":
            if rng.random() < rng.uniform(cfg.infect_p_trust_min, cfg.infect_p_trust_max):
                return True
        # OVERT nodes "push harder": always also roll the ambient rate,
        # on every tier, as an active retry on top of the tier-specific roll.
        return rng.random() < rng.uniform(cfg.infect_p_baseline_min, cfg.infect_p_baseline_max)
    if state.state.value == "INCUBATING":
        # Passive only: ambient rate regardless of tier.
        return rng.random() < rng.uniform(cfg.infect_p_baseline_min, cfg.infect_p_baseline_max)
    return False


def maybe_receive_exposure(state: "StateMachine", msg: dict, current_tick: int) -> bool:
    """Called by the receiver on any inbound heartbeat/trust_sync message.

    Returns True if this caused a HEALTHY -> INCUBATING transition.
    """
    if not msg.get("_exposure"):
        return False
    return state.infect(current_tick)
