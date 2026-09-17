"""Aggregator service (Spec sec. 9.3 + 9.2 proxy + static dashboard host).

Three jobs, all deliberately read-only or pass-through with respect to
game logic -- this process holds no authority over any node's actual
state, it only observes and relays:

1. Observability sink: nodes POST JSON events here one-way; they are
   logged, tallied (for display-only derived stats like cure progress and
   alive count) and broadcast to any connected dashboard over WebSocket.
2. Control-plane proxy: the dashboard cannot reach 30 separate node
   containers directly from a browser, so this exposes one HTTP surface
   that forwards snipe/cure/set_trust_threshold calls to the right node
   over the Docker-internal network. It also enforces the player's
   action *budget* (limited snipe/cure uses) -- that is meta/session
   policy, not node game state, so it belongs here rather than on any
   node.
3. Serves the static dashboard (single-page app) so `docker compose up`
   only needs one extra exposed port.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections import deque

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("aggregator")

MAX_EVENT_LOG = 3000
MAX_ALERT_FEED = 200


class Aggregator:
    def __init__(self, topology: dict, cure_runs_for_100: int, snipe_budget: int, cure_budget: int):
        self.topology = topology
        self.nodes_by_id = {n["id"]: n for n in topology["nodes"]}
        self.cure_runs_for_100 = cure_runs_for_100

        self.node_states: dict[str, dict] = {
            n["id"]: {"state": "HEALTHY", "ts": time.time()} for n in topology["nodes"]
        }
        self.event_log: deque = deque(maxlen=MAX_EVENT_LOG)
        self.alert_feed: deque = deque(maxlen=MAX_ALERT_FEED)
        self.successful_run_ids: set[str] = set()
        self.failed_run_count = 0
        self.death_point_counts: dict[str, int] = {}

        self.snipe_remaining = snipe_budget
        self.cure_remaining = cure_budget

        self.ws_clients: set[web.WebSocketResponse] = set()
        self._lock = asyncio.Lock()

    # -- derived / display-only stats -----------------------------------
    def cure_progress(self) -> float:
        return min(1.0, len(self.successful_run_ids) / self.cure_runs_for_100)

    def game_status(self) -> dict:
        states = [v["state"] for v in self.node_states.values()]
        alive = sum(1 for s in states if s != "DEAD")
        overt = sum(1 for s in states if s == "OVERT")
        non_dead_non_overt = sum(1 for s in states if s not in ("DEAD", "OVERT"))
        total_loss = alive > 0 and non_dead_non_overt == 0
        stable = overt == 0 and (self.cure_progress() >= 1.0 or
                                  sum(1 for s in states if s == "INCUBATING") == 0)
        return {
            "alive": alive,
            "total": len(states),
            "score": alive,
            "overt": overt,
            "loss": total_loss,
            "stable": stable,
            "cure_progress": self.cure_progress(),
            "snipe_remaining": self.snipe_remaining,
            "cure_remaining": self.cure_remaining,
        }

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "topology": self.topology,
            "node_states": self.node_states,
            "recent_events": list(self.event_log)[-200:],
            "recent_alerts": list(self.alert_feed),
            "game_status": self.game_status(),
        }

    # -- event ingestion --------------------------------------------------
    async def ingest(self, event: dict) -> None:
        async with self._lock:
            self.event_log.append(event)
            etype = event.get("type")
            node_id = event.get("node")

            if etype == "state_change" and node_id:
                self.node_states[node_id] = {"state": event.get("to"), "ts": event.get("ts")}
            elif etype == "cure_run_result":
                if event.get("success"):
                    self.successful_run_ids.add(event.get("run_id"))
                else:
                    self.failed_run_count += 1
                    died_at = event.get("died_at")
                    if died_at:
                        self.death_point_counts[died_at] = self.death_point_counts.get(died_at, 0) + 1
            elif etype in ("alert_raised", "alert_seen"):
                self.alert_feed.append(event)

        await self._broadcast({"type": "event", "event": event, "game_status": self.game_status()})

    async def _broadcast(self, payload: dict) -> None:
        if not self.ws_clients:
            return
        data = json.dumps(payload)
        dead = []
        for ws in self.ws_clients:
            try:
                await ws.send_str(data)
            except ConnectionResetError:
                dead.append(ws)
        for ws in dead:
            self.ws_clients.discard(ws)


routes = web.RouteTableDef()


@routes.post("/events")
async def post_event(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    try:
        event = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    await agg.ingest(event)
    return web.Response(status=204)


@routes.get("/topology")
async def get_topology(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    return web.json_response(agg.topology)


@routes.get("/status")
async def get_status(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    return web.json_response(agg.game_status())


@routes.get("/ws")
async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    agg: Aggregator = request.app["agg"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    agg.ws_clients.add(ws)
    await ws.send_str(json.dumps(agg.snapshot()))
    try:
        async for _msg in ws:
            pass  # dashboard doesn't send anything over this socket
    finally:
        agg.ws_clients.discard(ws)
    return ws


async def _forward_control(request: web.Request, endpoint: str) -> web.Response:
    agg: Aggregator = request.app["agg"]
    session: aiohttp.ClientSession = request.app["session"]
    node_id = request.match_info["node_id"]
    node = agg.nodes_by_id.get(node_id)
    if node is None:
        return web.json_response({"ok": False, "error": "unknown node"}, status=404)

    body = {}
    if request.can_read_body:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}

    url = f"http://{node_id}:{node.get('control_port', 8000)}/control/{endpoint}"
    try:
        async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            data = await resp.json()
            return web.json_response(data, status=resp.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=502)


@routes.post("/control/{node_id}/snipe")
async def control_snipe(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    if agg.snipe_remaining <= 0:
        return web.json_response({"ok": False, "error": "snipe budget exhausted"}, status=429)
    resp = await _forward_control(request, "snipe")
    if resp.status == 200:
        agg.snipe_remaining -= 1
    return resp


@routes.post("/control/{node_id}/cure")
async def control_cure(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    if agg.cure_progress() < 1.0:
        return web.json_response({"ok": False, "error": "cure not ready"}, status=409)
    if agg.cure_remaining <= 0:
        return web.json_response({"ok": False, "error": "cure budget exhausted"}, status=429)
    resp = await _forward_control(request, "cure")
    if resp.status == 200:
        agg.cure_remaining -= 1
    return resp


@routes.post("/control/trust_threshold")
async def control_trust_threshold_all(request: web.Request) -> web.Response:
    """Fan out set_trust_threshold to every node (Spec sec. 3: the
    threshold is a single global, player-set lever broadcast to all)."""
    agg: Aggregator = request.app["agg"]
    session: aiohttp.ClientSession = request.app["session"]
    try:
        body = await request.json()
        value = float(body["value"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "expected {'value': float}"}, status=400)

    async def _send(node_id: str) -> None:
        url = f"http://{node_id}:{agg.nodes_by_id[node_id].get('control_port', 8000)}/control/set_trust_threshold"
        try:
            async with session.post(url, json={"value": value}, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                await resp.release()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("threshold push to %s failed: %s", node_id, exc)

    await asyncio.gather(*(_send(nid) for nid in agg.nodes_by_id))
    return web.json_response({"ok": True, "threshold": value})


def build_app(topology: dict, dashboard_dir: str, cure_runs_for_100: int,
              snipe_budget: int, cure_budget: int) -> web.Application:
    app = web.Application()
    app["agg"] = Aggregator(topology, cure_runs_for_100, snipe_budget, cure_budget)
    app.add_routes(routes)
    if os.path.isdir(dashboard_dir):
        app.router.add_static("/", dashboard_dir, show_index=True, name="dashboard")

    async def _session_ctx(app):
        app["session"] = aiohttp.ClientSession()
        yield
        await app["session"].close()

    app.cleanup_ctx.append(_session_ctx)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", default=os.environ.get("TOPOLOGY_PATH", "topology/topology.json"))
    parser.add_argument("--dashboard-dir", default=os.environ.get("DASHBOARD_DIR", "dashboard"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--cure-runs-for-100", type=int,
                         default=int(os.environ.get("CURE_RUNS_FOR_100", 20)))
    parser.add_argument("--snipe-budget", type=int, default=int(os.environ.get("SNIPE_BUDGET", 6)))
    parser.add_argument("--cure-budget", type=int, default=int(os.environ.get("CURE_BUDGET", 6)))
    args = parser.parse_args()

    with open(args.topology, "r", encoding="utf-8") as f:
        topology = json.load(f)

    app = build_app(topology, args.dashboard_dir, args.cure_runs_for_100,
                     args.snipe_budget, args.cure_budget)
    logger.info("aggregator listening on 0.0.0.0:%s (%d nodes)", args.port, len(topology["nodes"]))
    web.run_app(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
