import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node.state import NodeState, StateMachine


def make_sm(seed=1):
    return StateMachine(random.Random(seed), incubation_min=10, incubation_max=10)


def test_starts_healthy():
    sm = make_sm()
    assert sm.state == NodeState.HEALTHY
    assert sm.is_alive
    assert not sm.is_infected
    assert sm.answers_heartbeat_normally


def test_infect_transitions_to_incubating():
    sm = make_sm()
    assert sm.infect(current_tick=5) is True
    assert sm.state == NodeState.INCUBATING
    assert sm.incubation_end_tick == 15
    assert sm.is_infected
    assert sm.answers_heartbeat_normally  # still covert on heartbeat channel


def test_infect_is_noop_when_not_healthy():
    sm = make_sm()
    sm.infect(0)
    assert sm.infect(0) is False
    assert sm.state == NodeState.INCUBATING


def test_turns_overt_only_after_timer():
    sm = make_sm()
    sm.infect(current_tick=0)  # ends at tick 10
    assert sm.maybe_turn_overt(5) is False
    assert sm.state == NodeState.INCUBATING
    assert sm.maybe_turn_overt(10) is True
    assert sm.state == NodeState.OVERT
    assert not sm.answers_heartbeat_normally


def test_cure_heals_infected_node():
    sm = make_sm()
    sm.infect(0)
    sm.maybe_turn_overt(10)
    correct, new_state = sm.cure()
    assert correct is True
    assert new_state == NodeState.CURED
    assert sm.state == NodeState.CURED


def test_cure_kills_healthy_node():
    sm = make_sm()
    correct, new_state = sm.cure()
    assert correct is False
    assert new_state == NodeState.DEAD


def test_snipe_kills_regardless_but_reports_correctness():
    healthy = make_sm()
    correct, new_state = healthy.snipe()
    assert correct is False
    assert new_state == NodeState.DEAD

    infected = make_sm()
    infected.infect(0)
    correct, new_state = infected.snipe()
    assert correct is True
    assert new_state == NodeState.DEAD


def test_relay_probability_bucket():
    sm = make_sm()
    assert sm.relay_success_probability_key == "healthy"
    sm.infect(0)
    assert sm.relay_success_probability_key == "infected"
