"""Cure-run origination and random-walk relay (Spec sec. 5).

Each run is a fully decentralized random walk: no node other than the one
currently holding the message knows the run exists, and no central party
tracks in-flight runs. Global cure progress is *derived*, after the fact,
by the observability plane counting `cure_run_result` events with
`success: true` -- it is not something any node computes or needs to know.

A node that fails to relay (probabilistically, weighted by its own
lifecycle state) is the one that reports its own failure: this is the
"weak evidence" signal the player uses to hunt for infected nodes (a node
that is consistently the death-point of many runs is statistically
suspicious, even though any single failure could be ordinary loss).
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .main import Node

logger = logging.getLogger("cure_relay")


async def maybe_originate(node: "Node") -> None:
    """Called once per tick by the game loop; only actually originates a
    run when this node's own randomized schedule says it's time."""
    if node.outbreak_known is None or node.tick < node.next_cure_run_tick:
        return
    node.next_cure_run_tick = node.tick + node.rng.randint(
        node.cfg.cure_run_interval_min, node.cfg.cure_run_interval_max)

    if node.state.state.value not in ("HEALTHY", "CURED"):
        return  # only HEALTHY/CURED nodes auto-originate runs

    run_id = uuid.uuid4().hex
    path = [node.cfg.node_id]
    hops_remaining = node.cfg.cure_run_length - 1

    if hops_remaining <= 0:
        return  # degenerate config, nothing to relay

    next_hop = _pick_unvisited_neighbor(node, path)
    if next_hop is None:
        await node.events.emit("cure_run_result", run_id=run_id, success=False,
                                path=path, died_at=node.cfg.node_id, reason="no_neighbors")
        return

    node.stats["runs_originated"] += 1
    await _forward(node, run_id, path, hops_remaining, next_hop)


async def handle_cure_run(node: "Node", msg: dict) -> None:
    if not node.state.is_alive:
        return  # dead nodes take no part (dispatch already reported it)

    run_id = msg.get("run_id")
    path = list(msg.get("path", []))
    hops_remaining = int(msg.get("hops_remaining", 0))

    if node.cfg.node_id in path:
        return  # defensive: never revisit a node within the same run

    p_relay = (node.cfg.relay_p_infected if node.state.is_infected
               else node.cfg.relay_p_healthy)
    success = node.rng.random() < p_relay

    if not success:
        node.stats["runs_dropped"] += 1
        await node.events.emit("cure_run_result", run_id=run_id, success=False,
                                path=path + [node.cfg.node_id], died_at=node.cfg.node_id,
                                reason="relay_drop")
        return

    new_path = path + [node.cfg.node_id]
    new_hops_remaining = hops_remaining - 1

    node.stats["runs_relayed"] += 1
    if new_hops_remaining <= 0:
        node.stats["runs_completed"] += 1
        await node.events.emit("cure_run_result", run_id=run_id, success=True,
                                path=new_path, died_at=None)
        return

    next_hop = _pick_unvisited_neighbor(node, new_path)
    if next_hop is None:
        await node.events.emit("cure_run_result", run_id=run_id, success=False,
                                path=new_path, died_at=node.cfg.node_id, reason="dead_end")
        return

    await _forward(node, run_id, new_path, new_hops_remaining, next_hop)


async def _forward(node: "Node", run_id: str, path: list[str], hops_remaining: int,
                   next_hop: str) -> None:
    from . import gossip
    msg = {"type": "cure_run", "run_id": run_id, "path": path, "hops_remaining": hops_remaining}
    tier = await gossip.route_message(node, next_hop, msg)
    if tier is None:
        # TCP delivery failed: the run is lost in transit rather than at a
        # node, so it doesn't count towards anyone's death-point tally.
        await node.events.emit("cure_run_result", run_id=run_id, success=False,
                               path=path, died_at=None, reason="send_failed",
                               lost_towards=next_hop)
        return
    event = "cure_run_started" if len(path) == 1 else "cure_run_hop"
    await node.events.emit(event, run_id=run_id, path=path, next_hop=next_hop,
                           tier=tier, hops_remaining=hops_remaining)


def _pick_unvisited_neighbor(node: "Node", path: list[str]) -> str | None:
    # skip neighbors the failure detector currently considers down: a run
    # sent there would just vanish
    candidates = [p for p in node.cfg.peers
                  if p not in path and not node.suspicion.is_down(p)]
    if not candidates:
        return None
    return node.rng.choice(candidates)
