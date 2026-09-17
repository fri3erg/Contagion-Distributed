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

    # Simulation clock
    tick_ms: int = field(default_factory=lambda: _env_int("TICK_MS", 250))

    # Lifecycle (Spec sec. 2, 14)
    incubation_min: int = field(default_factory=lambda: _env_int("INCUBATION_MIN", 60))
    incubation_max: int = field(default_factory=lambda: _env_int("INCUBATION_MAX", 180))

    # Cure relay (Spec sec. 5, 14)
    cure_run_length: int = field(default_factory=lambda: _env_int("CURE_RUN_LENGTH", 5))
    cure_runs_for_100: int = field(default_factory=lambda: _env_int("CURE_RUNS_FOR_100", 20))
    relay_p_healthy: float = field(default_factory=lambda: _env_float("RELAY_P_HEALTHY", 0.95))
    relay_p_infected: float = field(default_factory=lambda: _env_float("RELAY_P_INFECTED", 0.30))
    cure_run_interval_min: int = field(default_factory=lambda: _env_int("CURE_RUN_INTERVAL_MIN", 10))
    cure_run_interval_max: int = field(default_factory=lambda: _env_int("CURE_RUN_INTERVAL_MAX", 20))

    # Infection transmission (Spec sec. 4, 14)
    infect_p_baseline_min: float = field(default_factory=lambda: _env_float("INFECT_P_BASELINE_MIN", 0.01))
    infect_p_baseline_max: float = field(default_factory=lambda: _env_float("INFECT_P_BASELINE_MAX", 0.02))
    infect_p_trust_min: float = field(default_factory=lambda: _env_float("INFECT_P_TRUST_MIN", 0.15))
    infect_p_trust_max: float = field(default_factory=lambda: _env_float("INFECT_P_TRUST_MAX", 0.25))

    # Trust system (Spec sec. 3)
    trust_threshold_default: float = field(default_factory=lambda: _env_float("TRUST_THRESHOLD_DEFAULT", 0.5))
    trust_increment_heartbeat: float = field(default_factory=lambda: _env_float("TRUST_INCREMENT_HEARTBEAT", 0.01))
    trust_increment_sync: float = field(default_factory=lambda: _env_float("TRUST_INCREMENT_SYNC", 0.05))
    high_trust_sync_period_ticks: int = field(default_factory=lambda: _env_int("HIGH_TRUST_SYNC_PERIOD_TICKS", 4))

    # Alerts (Spec sec. 6)
    alert_ttl: int = field(default_factory=lambda: _env_int("ALERT_TTL", 5))
    alert_p_on_anomaly: float = field(default_factory=lambda: _env_float("ALERT_P_ON_ANOMALY", 0.6))

    # Heartbeat
    heartbeat_period_ticks: int = field(default_factory=lambda: _env_int("HEARTBEAT_PERIOD_TICKS", 1))
    heartbeat_ack_timeout_s: float = field(default_factory=lambda: _env_float("HEARTBEAT_ACK_TIMEOUT_S", 1.0))

    def rng(self) -> random.Random:
        # Per-process RNG; a seed can be supplied for reproducible runs.
        seed = os.environ.get("RNG_SEED")
        return random.Random(seed) if seed else random.Random()
