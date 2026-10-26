"""Tests for the aggregator's derived, display-only data: the infection
curve (one point per new median tick) and the per-game reset of it."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggregator.main import Aggregator

TOPOLOGY = {
    "nodes": [{"id": "node-00"}, {"id": "node-01"}, {"id": "node-02"}],
    "edges": [["node-00", "node-01"], ["node-01", "node-02"]],
    "patient_zero": "node-01",
}


def make_agg():
    return Aggregator(dict(TOPOLOGY), cure_runs_for_100=30, snipe_budget=6, cure_budget=6)


def status(agg, node, tick, state="HEALTHY", epoch=None):
    return {"type": "node_status", "node": node, "tick": tick, "state": state,
            "epoch": agg.epoch if epoch is None else epoch, "ts": 0,
            "tick_ms": agg.speed["tick_ms"], "paused": agg.speed["paused"]}


def feed(agg, events):
    async def go():
        for ev in events:
            await agg.ingest(ev)
    asyncio.run(go())


def test_series_appends_once_per_median_tick():
    agg = make_agg()
    feed(agg, [status(agg, n, 1) for n in ("node-00", "node-01", "node-02")])
    assert [p["tick"] for p in agg.series] == [1]
    feed(agg, [status(agg, n, 2) for n in ("node-00", "node-01", "node-02")])
    assert [p["tick"] for p in agg.series] == [1, 2]
    assert agg.series[-1]["HEALTHY"] == 3


def test_new_game_clears_series():
    agg = make_agg()
    feed(agg, [status(agg, n, 5) for n in ("node-00", "node-01", "node-02")])
    assert agg.series
    agg._clear_game()
    assert agg.series == []


def test_events_from_old_epoch_ignored():
    agg = make_agg()
    feed(agg, [status(agg, "node-00", 7, state="OVERT", epoch=agg.epoch + 1)])
    assert agg.node_states["node-00"]["state"] == "HEALTHY"
    assert agg.series == []
