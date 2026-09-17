import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from topology.generate_topology import build_topology, neighbors_of


def _is_connected(topology: dict) -> bool:
    adj = neighbors_of(topology)
    ids = list(adj.keys())
    seen = {ids[0]}
    stack = [ids[0]]
    while stack:
        cur = stack.pop()
        for nxt in adj[cur]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen == set(ids)


def test_generates_requested_node_count():
    topo = build_topology(n=30, k=4, rewire_p=0.15, seed=1)
    assert len(topo["nodes"]) == 30
    assert len({n["id"] for n in topo["nodes"]}) == 30  # unique ids


def test_graph_is_connected():
    for seed in range(5):
        topo = build_topology(n=30, k=4, rewire_p=0.2, seed=seed)
        assert _is_connected(topo), f"disconnected for seed={seed}"


def test_average_degree_is_close_to_k():
    topo = build_topology(n=30, k=4, rewire_p=0.15, seed=1)
    adj = neighbors_of(topo)
    avg_degree = sum(len(v) for v in adj.values()) / len(adj)
    assert 3.0 <= avg_degree <= 6.5


def test_no_self_loops_or_duplicate_edges():
    topo = build_topology(n=30, k=6, rewire_p=0.15, seed=2)
    seen = set()
    for a, b in topo["edges"]:
        assert a != b
        key = frozenset((a, b))
        assert key not in seen
        seen.add(key)


def test_patient_zero_is_a_real_node():
    topo = build_topology(n=30, k=4, rewire_p=0.15, seed=3)
    ids = {n["id"] for n in topo["nodes"]}
    assert topo["patient_zero"] in ids


def test_deterministic_with_same_seed():
    a = build_topology(n=20, k=4, rewire_p=0.15, seed=7)
    b = build_topology(n=20, k=4, rewire_p=0.15, seed=7)
    assert a == b
