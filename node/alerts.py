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

if TYPE_CHECKING:  # pragma: no cover
    from .main import Node

logger = logging.getLogger("alerts")


class SuspicionTracker:
    """Local-only tallies of anomalies this node has personally observed."""

    # A neighbor that misses this many heartbeats in a row is considered
    # down (simple heartbeat failure detector). Any reply brings it back.
    DOWN_AFTER_MISSES = 5

    def __init__(self):
        self.anomaly_counts: dict[str, int] = {}
        self.anomaly_streak: dict[str, int] = {}
        self.consecutive_misses: dict[str, int] = {}

    def record_anomaly(self, neighbor: str) -> int:
        self.anomaly_counts[neighbor] = self.anomaly_counts.get(neighbor, 0) + 1
        self.anomaly_streak[neighbor] = self.anomaly_streak.get(neighbor, 0) + 1
        return self.anomaly_counts[neighbor]

    def streak(self, neighbor: str) -> int:
        """Bad heartbeats in a row from this neighbor (any kind)."""
        return self.anomaly_streak.get(neighbor, 0)

    def record_good_reply(self, neighbor: str) -> None:
        self.anomaly_streak[neighbor] = 0

    def record_miss(self, neighbor: str) -> bool:
        """A heartbeat got no reply at all. Returns True if the neighbor
        has just crossed into 'down'."""
        misses = self.consecutive_misses.get(neighbor, 0) + 1
        self.consecutive_misses[neighbor] = misses
        return misses == self.DOWN_AFTER_MISSES

    def record_reply(self, neighbor: str) -> bool:
        """Any reply (even late or garbled) proves the neighbor is up.
        Returns True if it was considered down until now."""
        was_down = self.is_down(neighbor)
        self.consecutive_misses[neighbor] = 0
        return was_down

    def mark_down(self, neighbor: str) -> None:
        """The neighbor told us it is leaving: no need to wait for misses."""
        self.consecutive_misses[neighbor] = self.DOWN_AFTER_MISSES

    def is_down(self, neighbor: str) -> bool:
        return self.consecutive_misses.get(neighbor, 0) >= self.DOWN_AFTER_MISSES

    def down_neighbors(self) -> list[str]:
        return [n for n in self.consecutive_misses if self.is_down(n)]


async def raise_local_alert(node: "Node", about: str, reason: str) -> None:
    """Originate a fresh alert about `about`, based on this node's own
    first-hand observation, and gossip it out to neighbors."""
    # A node may keep complaining about the same neighbor, but not more
    # than once per cooldown: suspicion builds up as a rate over time.
    last = node.last_alert_tick.get(about)
    if reason != "killed" and last is not None and node.tick - last < node.cfg.alert_cooldown_ticks:
        return
    node.last_alert_tick[about] = node.tick
    node.alert_seq += 1
    node.seen_alerts.add((about, node.cfg.node_id, node.alert_seq))
    node.stats["alerts_raised"] += 1
    msg = {"type": "alert", "about": about, "reason": reason, "from": node.cfg.node_id,
           "seq": node.alert_seq, "ttl": node.cfg.alert_ttl}
    await node.events.emit("alert_raised", about=about, reason=reason)
    await _fan_out(node, msg, exclude=None)


async def handle_alert(node: "Node", msg: dict) -> None:
    about = msg.get("about")
    origin = msg.get("from")
    ttl = msg.get("ttl", 0)
    if about is None or origin is None:
        return

    dedup_key = (about, origin, msg.get("seq"))
    if dedup_key in node.seen_alerts:
        return  # already relayed this exact alert; stop the flood here
    node.seen_alerts.add(dedup_key)
    node.stats["alerts_seen"] += 1
    if msg.get("reason") in ("garbled", "killed"):
        # only zombies garble replies, and a node being shot means the
        # outbreak is real; a missing reply could just be loss
        node.learn_of_outbreak("alert", origin)

    await node.events.emit("alert_seen", about=about, reason=msg.get("reason"), origin=origin,
                           via=msg.get("via"), ttl=ttl)

    if about == node.cfg.node_id:
        return  # no need to keep gossiping an alert about ourselves further

    if ttl <= 0:
        return
    # don't echo it straight back to the neighbor we just got it from
    await _fan_out(node, {**msg, "ttl": ttl - 1}, exclude=msg.get("via"))


async def _fan_out(node: "Node", msg: dict, exclude: str | None) -> None:
    from . import gossip
    msg = {**msg, "via": node.cfg.node_id}
    for neighbor in node.cfg.peers:
        if neighbor == exclude:
            continue
        await gossip.route_message(node, neighbor, msg)
