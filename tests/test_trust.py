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


def test_bump_reports_tier_crossing():
    t = TrustTable(["node-01"], initial=0.1)
    t.set_threshold(0.5)
    assert t.bump("node-01", 0.1) is False
    assert t.bump("node-01", 0.35) is True
    assert t.tier("node-01") == "tcp"


def test_set_threshold_returns_flipped_neighbors():
    t = TrustTable(["node-01", "node-02"], initial=0.1)
    t.set_threshold(0.5)
    t.bump("node-02", 0.3)  # 0.4
    assert t.set_threshold(0.35) == ["node-02"]
    assert t.set_threshold(0.35) == []


def test_failure_detector_marks_down_after_consecutive_misses_and_recovers():
    from node.alerts import SuspicionTracker
    s = SuspicionTracker()
    for _ in range(SuspicionTracker.DOWN_AFTER_MISSES - 1):
        assert s.record_miss("node-01") is False
    assert s.record_miss("node-01") is True  # the Nth miss in a row
    assert s.is_down("node-01")
    assert s.record_reply("node-01") is True  # back up
    assert not s.is_down("node-01")
    s.record_miss("node-01")
    s.record_reply("node-01")
    s.record_miss("node-01")
    assert not s.is_down("node-01")  # misses must be consecutive
