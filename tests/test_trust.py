import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from node.trust import TrustTable


def test_default_scores_start_low():
    t = TrustTable(["node-01", "node-02"], initial=0.1)
    assert t.score("node-01") == 0.1
    assert t.score("unknown-node") == 0.0


def test_bump_is_capped_at_one():
    t = TrustTable(["node-01"], initial=0.1)
    for _ in range(200):
        t.bump("node-01", 0.05)
    assert t.score("node-01") == 1.0


def test_decay_is_capped_at_zero():
    t = TrustTable(["node-01"], initial=0.1)
    for _ in range(200):
        t.decay("node-01", 0.05)
    assert t.score("node-01") == 0.0


def test_tier_selection_follows_threshold():
    t = TrustTable(["node-01"], initial=0.1)
    t.set_threshold(0.5)
    assert t.uses_high_trust_tier("node-01") is False
    t.bump("node-01", 0.45)
    assert t.uses_high_trust_tier("node-01") is True


def test_set_threshold_is_clamped():
    t = TrustTable([])
    t.set_threshold(5)
    assert t.threshold == 1.0
    t.set_threshold(-5)
    assert t.threshold == 0.0
