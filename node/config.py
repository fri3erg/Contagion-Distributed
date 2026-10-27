"""Node configuration, loaded entirely from environment variables.

Every node container is built from the same image; what makes each node
distinct is the environment Docker Compose injects (NODE_ID, PEERS,
PATIENT_ZERO, ...). Keeping all tunables here means the game's balance
(Spec sec. 14) can be retuned from docker-compose.yml / topology generation
without touching code.
"""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    # Identity
    node_id: str = field(default_factory=lambda: os.environ["NODE_ID"])
    peers: list[str] = field(default_factory=lambda: [
        p for p in os.environ.get("PEERS", "").split(",") if p
    ])
    patient_zero: bool = field(default_factory=lambda: _env_bool("PATIENT_ZERO", False))

    # Networking (same port numbers on every node -- each container has its
    # own network namespace / hostname under Docker Compose DNS).
    udp_port: int = field(default_factory=lambda: _env_int("UDP_PORT", 7000))
    tcp_port: int = field(default_factory=lambda: _env_int("TCP_PORT", 7001))
    control_port: int = field(default_factory=lambda: _env_int("CONTROL_PORT", 8000))
    aggregator_url: str = field(default_factory=lambda: os.environ.get(
        "AGGREGATOR_URL", "http://aggregator:8000"))

    # Fraction of UDP datagrams the sender drops on purpose (lossy cheap tier)
    udp_loss: float = field(default_factory=lambda: _env_float("UDP_LOSS", 0.2))

    # Simulation clock
    tick_ms: int = field(default_factory=lambda: _env_int("TICK_MS", 5000))
    # Nodes boot frozen and wait for the player to press start
    start_paused: bool = field(default_factory=lambda: _env_bool("START_PAUSED", True))

    # Lifecycle (Spec sec. 2, 14)
    incubation_min: int = field(default_factory=lambda: _env_int("INCUBATION_MIN", 15))
    incubation_max: int = field(default_factory=lambda: _env_int("INCUBATION_MAX", 35))

    # Cure relay (Spec sec. 5, 14)
    cure_run_length: int = field(default_factory=lambda: _env_int("CURE_RUN_LENGTH", 3))
    cure_runs_for_100: int = field(default_factory=lambda: _env_int("CURE_RUNS_FOR_100", 12))
    relay_p_healthy: float = field(default_factory=lambda: _env_float("RELAY_P_HEALTHY", 0.95))
    relay_p_infected: float = field(default_factory=lambda: _env_float("RELAY_P_INFECTED", 0.30))
    cure_run_interval_min: int = field(default_factory=lambda: _env_int("CURE_RUN_INTERVAL_MIN", 4))
    cure_run_interval_max: int = field(default_factory=lambda: _env_int("CURE_RUN_INTERVAL_MAX", 8))
    # Only start cure runs once this node has noticed the outbreak
    cure_after_outbreak: bool = field(default_factory=lambda: _env_bool("CURE_AFTER_OUTBREAK", True))

    # Infection transmission (Spec sec. 4, 14)
    infect_p_baseline_min: float = field(default_factory=lambda: _env_float("INFECT_P_BASELINE_MIN", 0.01))
    infect_p_baseline_max: float = field(default_factory=lambda: _env_float("INFECT_P_BASELINE_MAX", 0.02))
    infect_p_trust_min: float = field(default_factory=lambda: _env_float("INFECT_P_TRUST_MIN", 0.05))
    infect_p_trust_max: float = field(default_factory=lambda: _env_float("INFECT_P_TRUST_MAX", 0.15))

    # Trust system (Spec sec. 3)
    trust_initial: float = field(default_factory=lambda: _env_float("TRUST_INITIAL", 0.2))
    trust_threshold_default: float = field(default_factory=lambda: _env_float("TRUST_THRESHOLD_DEFAULT", 0.5))
    trust_increment_heartbeat: float = field(default_factory=lambda: _env_float("TRUST_INCREMENT_HEARTBEAT", 0.03))
    trust_increment_sync: float = field(default_factory=lambda: _env_float("TRUST_INCREMENT_SYNC", 0.015))
    trust_penalty_anomaly: float = field(default_factory=lambda: _env_float("TRUST_PENALTY_ANOMALY", 0.1))
    # Chance an INCUBATING node silently skips a heartbeat reply (its "tell")
    incubating_hb_fail_p: float = field(default_factory=lambda: _env_float("INCUBATING_HB_FAIL_P", 0.1))
    high_trust_sync_period_ticks: int = field(default_factory=lambda: _env_int("HIGH_TRUST_SYNC_PERIOD_TICKS", 4))

    # Alerts (Spec sec. 6)
    alert_ttl: int = field(default_factory=lambda: _env_int("ALERT_TTL", 3))
    alert_cooldown_ticks: int = field(default_factory=lambda: _env_int("ALERT_COOLDOWN_TICKS", 5))
    # bad heartbeats in a row before a node raises an alert about a neighbor
    alert_after_streak: int = field(default_factory=lambda: _env_int("ALERT_AFTER_STREAK", 3))

    # Heartbeat
    heartbeat_period_ticks: int = field(default_factory=lambda: _env_int("HEARTBEAT_PERIOD_TICKS", 1))
    heartbeat_ack_timeout_s: float = field(default_factory=lambda: _env_float("HEARTBEAT_ACK_TIMEOUT_S", 1.0))

    # Observability: how often (seconds) a node dumps its full local state
    status_period_s: float = field(default_factory=lambda: _env_float("STATUS_PERIOD_S", 1.0))

    def rng(self) -> random.Random:
        # Per-process RNG; a seed can be supplied for reproducible runs.
        seed = os.environ.get("RNG_SEED")
        return random.Random(seed) if seed else random.Random()
