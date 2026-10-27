"""Tests for data-plane behavior in node.gossip: simulated UDP loss, trust
going down on bad behavior, and the hidden-infection heartbeat tell.

Real sockets are replaced by a transport that just records sendto() calls.
"""
import asyncio
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node import gossip
from node.alerts import SuspicionTracker
from node.state import StateMachine
from node.trust import TrustTable


@dataclass
class FakeConfig:
    node_id: str = "node-00"
    peers: list = field(default_factory=lambda: ["node-01", "node-02"])
    udp_port: int = 7000
    udp_loss: float = 0.0
    alert_after_streak: int = 99
    trust_penalty_anomaly: float = 0.1
    trust_increment_heartbeat: float = 0.0
    incubating_hb_fail_p: float = 0.0
    infect_p_baseline_min: float = 0.0
    infect_p_baseline_max: float = 0.0
    infect_p_trust_min: float = 0.0
    infect_p_trust_max: float = 0.0


class RecordingTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class FakeEvents:
    def __init__(self):
        self.emitted = []

    async def emit(self, event_type, **fields):
        self.emitted.append({"type": event_type, **fields})


class FakeNode:
    def __init__(self, **cfg):
        self.cfg = FakeConfig(**cfg)
        self.rng = random.Random(3)
        self.state = StateMachine(self.rng, 10, 10)
        self.trust = TrustTable(self.cfg.peers)
        self.suspicion = SuspicionTracker()
        self.events = FakeEvents()
        self.stats = Counter()
        self.udp_transport = RecordingTransport()
        self.epoch = 0
        self.tick = 0
        self.outbreak_known = None
        self._pending = []

    def spawn(self, coro):
        self._pending.append(coro)

    def learn_of_outbreak(self, how, source):
        pass

    def run_spawned(self):
        while self._pending:
            asyncio.run(self._pending.pop(0))


def run(coro):
    return asyncio.run(coro)


resolve_local = patch("node.gossip._resolve", new=AsyncMock(return_value=("127.0.0.1", 7000)))


# -- simulated UDP loss -------------------------------------------------

def test_udp_loss_one_drops_everything():
    node = FakeNode(udp_loss=1.0)
    with resolve_local:
        for _ in range(20):
            run(gossip._udp_send(node, "node-01", {"type": "heartbeat"}))
    assert node.udp_transport.sent == []
    assert node.stats["udp_dropped"] == 20


def test_udp_loss_zero_sends_everything():
    node = FakeNode(udp_loss=0.0)
    with resolve_local:
        for _ in range(20):
            run(gossip._udp_send(node, "node-01", {"type": "heartbeat"}))
    assert len(node.udp_transport.sent) == 20
    assert node.stats["udp_dropped"] == 0


# -- trust goes down on bad behavior ------------------------------------

def test_anomaly_decays_trust_and_reports_tier_flip():
    node = FakeNode(trust_penalty_anomaly=0.1)
    node.trust.set_threshold(0.5)
    node.trust.bump("node-01", 0.45)  # 0.1 + 0.45 = 0.55 -> tcp
    gossip._record_anomaly(node, "node-01", "timeout")
    gossip._record_anomaly(node, "node-01", "timeout")  # second in a row costs trust
    node.run_spawned()
    assert abs(node.trust.score("node-01") - 0.45) < 1e-9
    flips = [e for e in node.events.emitted if e["type"] == "tier_change"]
    assert len(flips) == 1
    assert flips[0]["tier"] == "udp"
    assert flips[0]["cause"] == "anomaly"


def test_trust_decay_clamps_at_zero():
    node = FakeNode(trust_penalty_anomaly=0.1)
    for _ in range(20):
        gossip._record_anomaly(node, "node-01", "timeout")
    node.run_spawned()
    assert node.trust.score("node-01") == 0.0


# -- the hidden-infection heartbeat tell --------------------------------

def _ping(node):
    msg = {"type": "heartbeat", "from": "node-01", "ts": 0, "nonce": "abc", "epoch": 0}
    with resolve_local:
        run(gossip._handle_heartbeat_request(node, msg))


def test_incubating_tell_drops_reply():
    node = FakeNode(incubating_hb_fail_p=1.0)
    node.state.infect(0)
    _ping(node)
    assert node.udp_transport.sent == []
    assert any(e.get("behavior") == "tell_dropped" for e in node.events.emitted)


def test_healthy_node_has_no_tell():
    node = FakeNode(incubating_hb_fail_p=1.0)
    _ping(node)
    assert len(node.udp_transport.sent) == 1


# -- "you got me": the victim tells the carrier it caught the infection --

def test_infected_receiver_tells_the_carrier():
    node = FakeNode()
    sent = AsyncMock(return_value={"type": "ack"})
    with patch("node.gossip.send_tcp_message", new=sent):
        run(gossip._receive_exposure(node, {"from": "node-01", "_exposure": True}, "heartbeat"))
        node.run_spawned()
    assert node.state.state.value == "INCUBATING"
    assert sent.call_args.args[1] == "node-01"
    assert sent.call_args.args[2]["type"] == "caught"


def test_exposure_without_effect_tells_nobody():
    node = FakeNode()
    node.state.infect(0)  # already infected
    sent = AsyncMock(return_value={"type": "ack"})
    with patch("node.gossip.send_tcp_message", new=sent):
        run(gossip._receive_exposure(node, {"from": "node-01", "_exposure": True}, "heartbeat"))
        node.run_spawned()
    sent.assert_not_called()


def test_caught_message_lets_the_carrier_turn():
    node = FakeNode()
    node.state.infect(0)
    run(gossip.dispatch_message(node, {"type": "caught", "from": "node-01", "epoch": 0}, "tcp"))
    assert node.state.has_spread


def test_single_lost_reply_costs_no_trust():
    node = FakeNode(trust_penalty_anomaly=0.1)
    before = node.trust.score("node-01")
    gossip._record_anomaly(node, "node-01", "timeout")
    node.run_spawned()
    assert node.trust.score("node-01") == before


def test_trust_starts_at_configured_level():
    from node.trust import TrustTable
    assert TrustTable(["node-01"], initial=0.2).score("node-01") == 0.2
