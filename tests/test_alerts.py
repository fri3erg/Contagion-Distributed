"""Tests for the suspicion signal: repeatable alerts with a cooldown, and
outbreak awareness coming only from garbled replies (things only zombies do).
"""
import asyncio
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node import alerts, gossip
from node.alerts import SuspicionTracker
from node.trust import TrustTable


@dataclass
class FakeConfig:
    node_id: str = "node-00"
    peers: list = field(default_factory=lambda: ["node-01", "node-02"])
    alert_ttl: int = 3
    alert_cooldown_ticks: int = 10
    alert_after_streak: int = 3
    trust_penalty_anomaly: float = 0.0


class FakeEvents:
    def __init__(self):
        self.emitted = []

    async def emit(self, event_type, **fields):
        self.emitted.append({"type": event_type, **fields})


class FakeNode:
    def __init__(self):
        self.cfg = FakeConfig()
        self.rng = random.Random(1)
        self.events = FakeEvents()
        self.stats = Counter()
        self.trust = TrustTable(self.cfg.peers)
        self.suspicion = SuspicionTracker()
        self.seen_alerts = set()
        self.last_alert_tick = {}
        self.alert_seq = 0
        self.tick = 0
        self.learned = []
        self._pending = []

    def spawn(self, coro):
        self._pending.append(coro)

    def run_spawned(self):
        while self._pending:
            asyncio.run(self._pending.pop(0))

    def learn_of_outbreak(self, how, source):
        self.learned.append((how, source))


def raised(node):
    return [e for e in node.events.emitted if e["type"] == "alert_raised"]


route = patch("node.gossip.route_message", new=AsyncMock(return_value="udp"))


def test_alert_not_repeated_within_cooldown():
    node = FakeNode()
    with route:
        asyncio.run(alerts.raise_local_alert(node, "node-01", "unresponsive"))
        node.tick = 5
        asyncio.run(alerts.raise_local_alert(node, "node-01", "unresponsive"))
    assert len(raised(node)) == 1


def test_alert_repeats_after_cooldown():
    node = FakeNode()
    with route:
        asyncio.run(alerts.raise_local_alert(node, "node-01", "unresponsive"))
        node.tick = 10
        asyncio.run(alerts.raise_local_alert(node, "node-01", "unresponsive"))
    assert len(raised(node)) == 2


def test_relayed_alert_deduplicated_per_sequence_number():
    node = FakeNode()
    msg = {"type": "alert", "about": "node-05", "reason": "unresponsive",
           "from": "node-03", "seq": 1, "ttl": 2}
    with route:
        asyncio.run(alerts.handle_alert(node, dict(msg)))
        asyncio.run(alerts.handle_alert(node, dict(msg)))
        asyncio.run(alerts.handle_alert(node, {**msg, "seq": 2}))
    seen = [e for e in node.events.emitted if e["type"] == "alert_seen"]
    assert len(seen) == 2


def test_garbled_reply_makes_node_aware():
    node = FakeNode()
    gossip._record_anomaly(node, "node-01", "garbled")
    with route:
        node.run_spawned()
    assert node.learned == [("garbled_heartbeat", "node-01")]


def test_missing_reply_does_not_make_node_aware():
    node = FakeNode()
    gossip._record_anomaly(node, "node-01", "timeout")
    gossip._record_anomaly(node, "node-01", "late")
    with route:
        node.run_spawned()
    assert node.learned == []


def test_only_garbled_alerts_spread_awareness():
    node = FakeNode()
    with route:
        asyncio.run(alerts.handle_alert(node, {"type": "alert", "about": "node-05",
                                               "reason": "unresponsive", "from": "node-03", "seq": 1, "ttl": 0}))
        assert node.learned == []
        asyncio.run(alerts.handle_alert(node, {"type": "alert", "about": "node-05",
                                               "reason": "garbled", "from": "node-03", "seq": 2, "ttl": 0}))
    assert node.learned == [("alert", "node-03")]


# -- alerts need a streak of bad heartbeats ----------------------------

def _anomalies(node, kinds):
    for kind in kinds:
        gossip._record_anomaly(node, "node-01", kind)
    with route:
        node.run_spawned()


def test_three_bad_heartbeats_in_a_row_raise_an_alert():
    node = FakeNode()
    _anomalies(node, ["timeout", "late"])
    assert raised(node) == []
    _anomalies(node, ["timeout"])
    assert len(raised(node)) == 1
    assert raised(node)[0]["reason"] == "unresponsive"


def test_good_reply_breaks_the_streak():
    node = FakeNode()
    _anomalies(node, ["timeout", "timeout"])
    node.suspicion.record_good_reply("node-01")
    _anomalies(node, ["timeout"])
    assert raised(node) == []


def test_garbled_reply_alerts_immediately():
    node = FakeNode()
    _anomalies(node, ["garbled"])
    assert len(raised(node)) == 1
    assert raised(node)[0]["reason"] == "garbled"


# -- graceful leave: a sniped node says goodbye --------------------------

def test_leave_message_marks_neighbor_down_and_makes_aware():
    node = FakeNode()
    with route:
        asyncio.run(gossip._handle_leave(node, {"type": "leave", "from": "node-01", "epoch": 0}))
        node.run_spawned()
    assert node.suspicion.is_down("node-01")
    assert ("neighbor_killed", "node-01") in node.learned
    assert raised(node)[0]["reason"] == "killed"


def test_killed_alert_spreads_awareness_even_within_cooldown():
    node = FakeNode()
    with route:
        asyncio.run(alerts.raise_local_alert(node, "node-01", "unresponsive"))
        asyncio.run(alerts.raise_local_alert(node, "node-01", "killed"))
        asyncio.run(alerts.handle_alert(node, {"type": "alert", "about": "node-05",
                                               "reason": "killed", "from": "node-03", "seq": 9, "ttl": 0}))
    assert [e["reason"] for e in raised(node)] == ["unresponsive", "killed"]
    assert ("alert", "node-03") in node.learned


def test_announce_leave_tells_every_neighbor_even_once_dead():
    node = FakeNode()
    node.state = type("S", (), {"is_alive": False})()
    node.epoch = 0
    sent = AsyncMock(return_value={"type": "ack"})
    with patch("node.gossip.send_tcp_message", new=sent):
        asyncio.run(gossip.announce_leave(node))
    assert sorted(c.args[1] for c in sent.call_args_list) == ["node-01", "node-02"]
    assert all(c.args[2]["type"] == "leave" and c.kwargs.get("allow_dead") for c in sent.call_args_list)
