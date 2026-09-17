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
        self.state = StateMachine(self.rng, cfg.incubation_min, cfg.incubation_max)
        self.trust = TrustTable(cfg.peers)
        self.trust.set_threshold(cfg.trust_threshold_default)
        self.suspicion = SuspicionTracker()
        self.events = EventEmitter(cfg.node_id, cfg.aggregator_url, session)

        self.seen_alerts: set[tuple[str, str]] = set()
        self.pending_heartbeats: dict[str, dict] = {}
        self.udp_transport: asyncio.DatagramTransport | None = None

        self.tick: int = 0
        self.next_cure_run_tick: int = self.rng.randint(
            cfg.cure_run_interval_min, cfg.cure_run_interval_max)

    async def tick_once(self) -> None:
        self.tick += 1

        if not self.state.is_alive:
            return

        if self.state.maybe_turn_overt(self.tick):
            await self.events.emit("state_change", from_state="INCUBATING",
                                    to="OVERT", cause="incubation_timer")

        if self.tick % self.cfg.heartbeat_period_ticks == 0:
            for neighbor in self.cfg.peers:
                asyncio.create_task(gossip.send_heartbeat(self, neighbor))

        if self.tick % self.cfg.high_trust_sync_period_ticks == 0:
            for neighbor in self.cfg.peers:
                if self.trust.uses_high_trust_tier(neighbor):
                    asyncio.create_task(gossip.send_trust_sync(self, neighbor))

        await gossip.check_heartbeat_timeouts(self)
        await cure_relay.maybe_originate(self)


async def run() -> None:
    cfg = Config()
    logger.info("starting node %s, peers=%s, patient_zero=%s",
                cfg.node_id, cfg.peers, cfg.patient_zero)

    async with aiohttp.ClientSession() as session:
        node = Node(cfg, session)

        if cfg.patient_zero:
            node.state.infect(node.tick)
            await node.events.emit("state_change", from_state="HEALTHY",
                                    to="INCUBATING", cause="patient_zero")
        else:
            await node.events.emit("state_change", from_state=None, to="HEALTHY", cause="startup")

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
                try:
                    await node.tick_once()
                except Exception:  # noqa: BLE001 - one bad tick must not kill the node
                    logger.exception("error in tick %s", node.tick)
                await asyncio.sleep(cfg.tick_ms / 1000)

        tick_task = asyncio.create_task(_tick_loop())

        try:
            await stop_event.wait()
        finally:
            tick_task.cancel()
            transport.close()
            tcp_server.close()
            await tcp_server.wait_closed()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run())
