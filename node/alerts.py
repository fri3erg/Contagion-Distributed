"""Suspicion broadcast: a gossip epidemic of its own (Spec sec. 6).

Independent of the infection and cure-relay traffic, but riding the same
two-tier transport. A node that notices something odd about a neighbor
(a heartbeat anomaly, or a report from a third party) floods an alert
message outward with a decrementing TTL, deduplicated by (about, from)
pairs it has already seen so the flood terminates.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import gossip  # noqa: F401  (imported lazily inside functions to avoid cycles)

if TYPE_CHECKING:  # pragma: no cover
    from .main import Node

logger = logging.getLogger("alerts")


class SuspicionTracker:
    """Local-only tallies of anomalies this node has personally observed."""

    def __init__(self):
        self.anomaly_counts: dict[str, int] = {}

    def record_anomaly(self, neighbor: str) -> int:
        self.anomaly_counts[neighbor] = self.anomaly_counts.get(neighbor, 0) + 1
        return self.anomaly_counts[neighbor]


async def raise_local_alert(node: "Node", about: str, reason: str) -> None:
    """Originate a fresh alert about `about`, based on this node's own
    first-hand observation, and gossip it out to neighbors."""
    key = (about, node.cfg.node_id)
    if key in node.seen_alerts:
        return
    node.seen_alerts.add(key)
    msg = {"type": "alert", "about": about, "reason": reason, "from": node.cfg.node_id,
           "ttl": node.cfg.alert_ttl}
    await node.events.emit("alert_raised", about=about, reason=reason)
    await _fan_out(node, msg, exclude=None)


async def handle_alert(node: "Node", msg: dict) -> None:
    about = msg.get("about")
    origin = msg.get("from")
    ttl = msg.get("ttl", 0)
    if about is None or origin is None:
        return

    dedup_key = (about, origin)
    if dedup_key in node.seen_alerts:
        return  # already relayed this exact alert; stop the flood here
    node.seen_alerts.add(dedup_key)

    await node.events.emit("alert_seen", about=about, reason=msg.get("reason"), origin=origin)

    if about == node.cfg.node_id:
        return  # no need to keep gossiping an alert about ourselves further

    if ttl <= 0:
        return
    forwarded = {**msg, "ttl": ttl - 1}
    await _fan_out(node, forwarded, exclude=msg.get("_from_neighbor"))


async def _fan_out(node: "Node", msg: dict, exclude: str | None) -> None:
    from . import gossip as gossip_mod
    for neighbor in node.cfg.peers:
        if neighbor == exclude:
            continue
        await gossip_mod.route_message(node, neighbor, msg)
