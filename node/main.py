"""Node process entrypoint.

Wires together the state machine, trust table, data-plane transports,
control-plane HTTP endpoint and observability emitter into one asyncio
process, and drives the simulated tick loop. Each node is a fully
independent OS process (one per container); this module holds the only
copy of this node's state that will ever exist.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from collections import Counter

import aiohttp
from aiohttp import web

from . import control_api, cure_relay, gossip
from .config import Config
from .events import EventEmitter
from .alerts import SuspicionTracker
from .state import StateMachine
from .trust import TrustTable

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("node")


class Node:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.rng = cfg.rng()
        self.events = EventEmitter(cfg.node_id, cfg.aggregator_url, session,
                                   tick_fn=lambda: self.tick, epoch_fn=lambda: self.epoch)
        self._tasks: set[asyncio.Task] = set()
        self.udp_transport: asyncio.DatagramTransport | None = None

        # Simulation speed, changeable at runtime through the control plane.
        self.tick_ms: int = cfg.tick_ms
        self.paused: bool = cfg.start_paused
        self.is_patient_zero: bool = cfg.patient_zero
        # Game number. Every data-plane message carries it and messages from
        # another epoch are dropped, so a restart can't be polluted by
        # traffic from the previous game still in flight.
        self.epoch: int = 0
        self._new_game()

    def _new_game(self) -> None:
        cfg = self.cfg
        self.state = StateMachine(self.rng, cfg.incubation_min, cfg.incubation_max)
        self.trust = TrustTable(cfg.peers, initial=cfg.trust_initial)
        self.trust.set_threshold(cfg.trust_threshold_default)
        self.suspicion = SuspicionTracker()
        # local counters, only reported through node_status for debugging
        self.stats: Counter = Counter()
        self.seen_alerts: set[tuple] = set()
        self.last_alert_tick: dict[str, int] = {}
        self.alert_seq: int = 0
        self.pending_heartbeats: dict[str, dict] = {}
        self.tick: int = 0
        self.next_cure_run_tick: int = self.rng.randint(
            cfg.cure_run_interval_min, cfg.cure_run_interval_max)
        # Whether this node has noticed the outbreak yet (first-hand via a
        # bad heartbeat, or second-hand via an alert). Until then it doesn't
        # start cure runs. Purely local knowledge, nobody announces it.
        self.outbreak_known: dict | None = None if cfg.cure_after_outbreak else {
            "tick": 0, "how": "always_on", "source": None}
        if self.is_patient_zero:
            self.state.infect(self.tick)

    def learn_of_outbreak(self, how: str, source: str | None) -> None:
        if self.outbreak_known is not None:
            return
        self.outbreak_known = {"tick": self.tick, "how": how, "source": source}
        # first run after a normal random delay, so a wave of alerts doesn't
        # make every node start a run on the same tick
        self.next_cure_run_tick = self.tick + self.rng.randint(
            self.cfg.cure_run_interval_min, self.cfg.cure_run_interval_max)
        self.spawn(self.events.emit("outbreak_detected", how=how, source=source,
                                    first_run_at_tick=self.next_cure_run_tick))

    async def announce_state(self) -> None:
        if self.is_patient_zero:
            await self.events.emit("state_change", from_state="HEALTHY",
                                   to="INCUBATING", cause="patient_zero")
        else:
            await self.events.emit("state_change", from_state=None, to="HEALTHY", cause="startup")

    async def reset(self, epoch: int, patient_zero: bool) -> None:
        """Throw away the current game and start a fresh one (control plane).
        Anything still running for the old game is cancelled."""
        for task in list(self._tasks):
            task.cancel()
        self.epoch = epoch
        self.is_patient_zero = patient_zero
        self._new_game()
        await self.announce_state()

    def spawn(self, coro) -> None:
        """Run a coroutine in the background so slow sends (TCP timeouts,
        event POSTs) never stall the tick loop. Keeps a reference so the
        task isn't garbage collected mid-flight."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def tick_once(self) -> None:
        self.tick += 1

        if not self.state.is_alive:
            return

        if self.state.maybe_turn_overt(self.tick):
            self.spawn(self.events.emit("state_change", from_state="INCUBATING",
                                        to="OVERT", cause="incubation_timer"))

        if self.tick % self.cfg.heartbeat_period_ticks == 0:
            for neighbor in self.cfg.peers:
                self.spawn(gossip.send_heartbeat(self, neighbor))

        if self.tick % self.cfg.high_trust_sync_period_ticks == 0:
            for neighbor in self.cfg.peers:
                if self.trust.uses_high_trust_tier(neighbor):
                    self.spawn(gossip.send_trust_sync(self, neighbor))

        gossip.check_heartbeat_timeouts(self)
        if self.outbreak_known is not None and self.tick >= self.next_cure_run_tick:
            self.spawn(cure_relay.maybe_originate(self))

    async def emit_status(self) -> None:
        """Periodic full dump of this node's local view, for the debug
        dashboard. Purely observability: nobody acts on it."""
        end = self.state.incubation_end_tick
        await self.events.emit(
            "node_status",
            state=self.state.state.value,
            incubation_end_tick=end,
            ticks_to_overt=(end - self.tick) if end is not None and self.state.state.value == "INCUBATING" else None,
            threshold=self.trust.threshold,
            trust={n: round(self.trust.score(n), 3) for n in self.cfg.peers},
            tiers={n: self.trust.tier(n) for n in self.cfg.peers},
            anomalies=dict(self.suspicion.anomaly_counts),
            stats=dict(self.stats),
            pending_heartbeats=len(self.pending_heartbeats),
            next_cure_run_tick=self.next_cure_run_tick,
            tick_ms=self.tick_ms,
            paused=self.paused,
            patient_zero=self.is_patient_zero,
            outbreak_known=self.outbreak_known,
            waiting_for_victim=self.state.waiting_for_victim,
            overt_cap_tick=self.state.overt_cap_tick,
            down=self.suspicion.down_neighbors(),
        )


async def run() -> None:
    cfg = Config()
    logger.info("starting node %s, peers=%s, patient_zero=%s",
                cfg.node_id, cfg.peers, cfg.patient_zero)

    async with aiohttp.ClientSession() as session:
        node = Node(cfg, session)
        await node.announce_state()

        loop = asyncio.get_running_loop()

        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: gossip.HeartbeatProtocol(node),
            local_addr=("0.0.0.0", cfg.udp_port),
        )

        async def _tcp_handler(reader, writer):
            await gossip.handle_tcp_connection(node, reader, writer)

        tcp_server = await asyncio.start_server(_tcp_handler, "0.0.0.0", cfg.tcp_port)

        app = control_api.build_app(node)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", cfg.control_port)
        await site.start()

        stop_event = asyncio.Event()

        def _handle_signal():
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                pass  # Windows dev fallback; not used inside containers

        async def _tick_loop():
            while not stop_event.is_set():
                if node.paused:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    await node.tick_once()
                except Exception:  # noqa: BLE001 - one bad tick must not kill the node
                    logger.exception("error in tick %s", node.tick)
                await asyncio.sleep(node.tick_ms / 1000)

        async def _status_loop():
            # wall-clock based, so the debug view stays fresh even when the
            # simulation is slowed down or paused
            while not stop_event.is_set():
                node.spawn(node.emit_status())
                await asyncio.sleep(cfg.status_period_s)

        tick_task = asyncio.create_task(_tick_loop())
        status_task = asyncio.create_task(_status_loop())

        try:
            await stop_event.wait()
        finally:
            tick_task.cancel()
            status_task.cancel()
            transport.close()
            tcp_server.close()
            await tcp_server.wait_closed()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
