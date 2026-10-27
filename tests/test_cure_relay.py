"""Unit tests for the cure-relay random walk (Spec sec. 5).

Networking is stubbed out (node.gossip.route_message is monkeypatched) so
these tests exercise only the relay/probability/event logic, not real
sockets.
"""
import asyncio
import random
from collections import Counter
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node import cure_relay
from node.alerts import SuspicionTracker
from node.state import StateMachine
from node.trust import TrustTable


@dataclass
class FakeConfig:
    node_id: str
    peers: list
    cure_run_length: int = 5
    relay_p_healthy: float = 1.0
    relay_p_infected: float = 0.0
    cure_run_interval_min: int = 10
    cure_run_interval_max: int = 10


class FakeEvents:
    def __init__(self):
        self.emitted = []

    async def emit(self, event_type, **fields):
        self.emitted.append({"type": event_type, **fields})


class FakeNode:
    def __init__(self, node_id, peers, seed=1, relay_p_healthy=1.0, relay_p_infected=0.0):
        self.cfg = FakeConfig(node_id, peers, relay_p_healthy=relay_p_healthy,
                               relay_p_infected=relay_p_infected)
        self.rng = random.Random(seed)
        self.state = StateMachine(self.rng, 60, 180)
        self.tick = 0
        self.next_cure_run_tick = 0
        self.events = FakeEvents()
        self.stats = Counter()
        self.outbreak_known = None
        self.suspicion = SuspicionTracker()
        # every neighbor trusted unless a test says otherwise (cure runs need TCP)
        self.trust = TrustTable(peers, initial=1.0)
        self.trust.set_threshold(0.5)


def run(coro):
    return asyncio.run(coro)


def test_healthy_node_always_relays_when_p_is_one():
    node = FakeNode("node-02", ["node-03", "node-04"], relay_p_healthy=1.0)
    msg = {"run_id": "r1", "path": ["node-00", "node-01"], "hops_remaining": 2}
    with patch("node.gossip.route_message", new=AsyncMock(return_value="udp")) as route:
        run(cure_relay.handle_cure_run(node, msg))
    # no result yet, still in flight -- only the hop is reported
    assert [e["type"] for e in node.events.emitted] == ["cure_run_hop"]
    assert node.events.emitted[0]["tier"] == "udp"
    route.assert_called_once()
    forwarded_msg = route.call_args.args[2]
    assert forwarded_msg["path"] == ["node-00", "node-01", "node-02"]
    assert forwarded_msg["hops_remaining"] == 1


def test_failed_tcp_send_reports_run_lost_in_transit():
    node = FakeNode("node-02", ["node-03"], relay_p_healthy=1.0)
    msg = {"run_id": "r1", "path": ["node-00", "node-01"], "hops_remaining": 2}
    with patch("node.gossip.route_message", new=AsyncMock(return_value=None)):
        run(cure_relay.handle_cure_run(node, msg))
    result = node.events.emitted[-1]
    assert result["type"] == "cure_run_result"
    assert result["reason"] == "send_failed"
    assert result["died_at"] is None
    assert result["lost_towards"] == "node-03"


def test_infected_node_always_drops_when_p_is_zero():
    node = FakeNode("node-02", ["node-03"], relay_p_infected=0.0)
    node.state.infect(0)
    node.state.maybe_turn_overt(10 ** 9)  # doesn't matter, still infected
    msg = {"run_id": "r1", "path": ["node-00", "node-01"], "hops_remaining": 2}
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.handle_cure_run(node, msg))
    route.assert_not_called()
    assert len(node.events.emitted) == 1
    result = node.events.emitted[0]
    assert result["type"] == "cure_run_result"
    assert result["success"] is False
    assert result["died_at"] == "node-02"


def test_run_completes_when_hops_remaining_hits_zero():
    node = FakeNode("node-04", ["node-05"], relay_p_healthy=1.0)
    msg = {"run_id": "r1", "path": ["node-00", "node-01", "node-02", "node-03"], "hops_remaining": 1}
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.handle_cure_run(node, msg))
    route.assert_not_called()
    result = node.events.emitted[0]
    assert result["success"] is True
    assert result["path"] == ["node-00", "node-01", "node-02", "node-03", "node-04"]


def test_dead_end_reports_failure():
    node = FakeNode("node-02", ["node-00", "node-01"], relay_p_healthy=1.0)  # all peers already visited
    msg = {"run_id": "r1", "path": ["node-00", "node-01"], "hops_remaining": 2}
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.handle_cure_run(node, msg))
    route.assert_not_called()
    result = node.events.emitted[0]
    assert result["success"] is False
    assert result["reason"] == "dead_end"


def test_dead_node_ignores_cure_runs():
    node = FakeNode("node-02", ["node-03"], relay_p_healthy=1.0)
    node.state.snipe()  # kill it
    msg = {"run_id": "r1", "path": ["node-00"], "hops_remaining": 3}
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.handle_cure_run(node, msg))
    route.assert_not_called()
    assert node.events.emitted == []


def test_never_revisits_a_node_in_the_same_run():
    node = FakeNode("node-02", ["node-03"], relay_p_healthy=1.0)
    msg = {"run_id": "r1", "path": ["node-00", "node-02"], "hops_remaining": 2}
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.handle_cure_run(node, msg))
    route.assert_not_called()
    assert node.events.emitted == []  # defensively dropped, not even reported


def test_origination_only_from_healthy_or_cured():
    node = FakeNode("node-02", ["node-03", "node-04"])
    node.state.infect(0)  # INCUBATING now
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.maybe_originate(node))
    route.assert_not_called()
    assert node.events.emitted == []


def test_origination_from_healthy_sends_first_hop():
    node = FakeNode("node-02", ["node-03", "node-04"], relay_p_healthy=1.0)
    node.outbreak_known = {"tick": 0, "how": "alert", "source": "node-05"}
    with patch("node.gossip.route_message", new=AsyncMock()) as route:
        run(cure_relay.maybe_originate(node))
    route.assert_called_once()
    neighbor, msg = route.call_args.args[1], route.call_args.args[2]
    assert neighbor in ("node-03", "node-04")
    assert msg["path"] == ["node-02"]
    assert msg["hops_remaining"] == node.cfg.cure_run_length - 1


def test_no_cure_runs_before_node_knows_of_outbreak():
    node = FakeNode("node-00", ["node-01", "node-02"])
    node.tick = 50  # well past its scheduled run
    with patch("node.gossip.route_message", new=AsyncMock(return_value="udp")) as route:
        run(cure_relay.maybe_originate(node))
    route.assert_not_called()
    assert node.events.emitted == []


def test_cure_runs_start_once_node_knows_of_outbreak():
    node = FakeNode("node-00", ["node-01", "node-02"])
    node.tick = 50
    node.outbreak_known = {"tick": 40, "how": "alert", "source": "node-05"}
    with patch("node.gossip.route_message", new=AsyncMock(return_value="udp")) as route:
        run(cure_relay.maybe_originate(node))
    route.assert_called_once()
    assert node.events.emitted[0]["type"] == "cure_run_started"


def test_relay_skips_neighbors_detected_as_down():
    node = FakeNode("node-02", ["node-03", "node-04"], relay_p_healthy=1.0)
    for _ in range(SuspicionTracker.DOWN_AFTER_MISSES):
        node.suspicion.record_miss("node-03")
    msg = {"run_id": "r1", "path": ["node-00"], "hops_remaining": 3}
    with patch("node.gossip.route_message", new=AsyncMock(return_value="udp")) as route:
        run(cure_relay.handle_cure_run(node, msg))
    assert route.call_args.args[1] == "node-04"


# -- cure runs only travel over trusted (TCP) links ---------------------

def test_relay_skips_untrusted_neighbors():
    node = FakeNode("node-02", ["node-03", "node-04"], relay_p_healthy=1.0)
    node.trust.decay("node-03", 1.0)  # node-03 drops below the threshold
    msg = {"run_id": "r1", "path": ["node-00"], "hops_remaining": 3}
    with patch("node.gossip.route_message", new=AsyncMock(return_value="tcp")) as route:
        run(cure_relay.handle_cure_run(node, msg))
    assert route.call_args.args[1] == "node-04"


def test_run_dies_when_no_trusted_link_is_left():
    node = FakeNode("node-02", ["node-03"], relay_p_healthy=1.0)
    node.trust.decay("node-03", 1.0)
    msg = {"run_id": "r1", "path": ["node-00"], "hops_remaining": 3}
    with patch("node.gossip.route_message", new=AsyncMock(return_value="tcp")) as route:
        run(cure_relay.handle_cure_run(node, msg))
    route.assert_not_called()
    assert node.events.emitted[-1]["reason"] == "no_trusted_link"


def test_no_run_starts_without_a_trusted_link():
    node = FakeNode("node-02", ["node-03"])
    node.trust.decay("node-03", 1.0)
    node.outbreak_known = {"tick": 0, "how": "alert", "source": "node-05"}
    with patch("node.gossip.route_message", new=AsyncMock(return_value="tcp")) as route:
        run(cure_relay.maybe_originate(node))
    route.assert_not_called()
    assert node.events.emitted == []
