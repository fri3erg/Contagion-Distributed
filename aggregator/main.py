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
import random
import time
from collections import deque

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("aggregator")

MAX_EVENT_LOG = 20000
MAX_ALERT_FEED = 200
STATES = ("HEALTHY", "INCUBATING", "OVERT", "CURED", "DEAD")


class Aggregator:
    def __init__(self, topology: dict, cure_runs_for_100: int, snipe_budget: int, cure_budget: int):
        self.topology = topology
        self.nodes_by_id = {n["id"]: n for n in topology["nodes"]}
        self.cure_runs_for_100 = cure_runs_for_100
        self.snipe_budget = snipe_budget
        self.cure_budget = cure_budget

        # Session state of the playthrough (control plane, not game logic):
        # which game we're on, and the speed the player asked for. Nodes
        # boot at epoch 0; the aggregator always picks a fresh random epoch,
        # so any node still on an older game (including after the aggregator
        # itself restarted) shows up as out of date and gets reset by
        # _reconcile into this game.
        self.epoch = self._new_epoch()
        self.game_number = 1
        self.patient_zero = topology["patient_zero"]
        self.speed = {"tick_ms": int(os.environ.get("TICK_MS", 5000)), "paused": True}
        self.started = False
        self.over: str | None = None  # "loss" / "stable" / "time_up" once the game has ended
        self.max_ticks = int(os.environ.get("MAX_TICKS", 400))
        self.reset_at = 0.0
        self._last_push: dict[str, float] = {}
        self.session: aiohttp.ClientSession | None = None

        self.ws_clients: set[web.WebSocketResponse] = set()
        self._lock = asyncio.Lock()
        self._clear_game()

    def _clear_game(self) -> None:
        self.node_states: dict[str, dict] = {
            nid: {"state": "INCUBATING" if nid == self.patient_zero else "HEALTHY", "ts": time.time()}
            for nid in self.nodes_by_id
        }
        self.event_log: deque = deque(maxlen=MAX_EVENT_LOG)
        self.alert_feed: deque = deque(maxlen=MAX_ALERT_FEED)
        self.successful_run_ids: set[str] = set()
        self.failed_run_count = 0
        self.death_point_counts: dict[str, int] = {}

        # Debug-only bookkeeping for the dashboard. All of it is derived
        # from what nodes reported; none of it is ever sent back to a node.
        self.node_status: dict[str, dict] = {}
        self.infected_by: dict[str, dict] = {}
        self.anomalies_about: dict[str, int] = {}
        self.alerts_about: dict[str, int] = {}
        self.run_outcomes: dict[str, int] = {}
        self.runs_started = 0
        # infection curve for the end screen: state counts per median tick
        self.series: list[dict] = []

        self.snipe_remaining = self.snipe_budget
        self.cure_remaining = self.cure_budget

    async def new_game(self) -> None:
        """Restart: bump the epoch, pick a new patient zero, wipe derived
        state, then tell every node to reset (paused, waiting for start)."""
        async with self._lock:
            self.epoch = self._new_epoch()
            self.game_number += 1
            self.patient_zero = random.choice(list(self.nodes_by_id))
            self.topology["patient_zero"] = self.patient_zero
            self.speed["paused"] = True
            self.started = False
            self.over = None
            self.reset_at = time.time()
            self._clear_game()
        await self.send_to_all("reset", lambda nid: self._reset_body(nid))
        await self._broadcast(self.snapshot())

    def _new_epoch(self) -> int:
        while True:
            epoch = random.randint(1, 2**31 - 1)
            if epoch != getattr(self, "epoch", None):
                return epoch

    def _reset_body(self, node_id: str) -> dict:
        return {"epoch": self.epoch, "patient_zero": node_id == self.patient_zero, **self.speed}

    async def send_to_all(self, endpoint: str, body_fn) -> None:
        await asyncio.gather(*(self.send_to(nid, endpoint, body_fn(nid)) for nid in self.nodes_by_id))

    async def send_to(self, node_id: str, endpoint: str, body: dict) -> None:
        if self.session is None:
            return
        url = f"http://{node_id}:{self.nodes_by_id[node_id].get('control_port', 8000)}/control/{endpoint}"
        try:
            async with self.session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                await resp.release()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("%s push to %s failed: %s", endpoint, node_id, exc)

    def _reconcile(self, node_id: str, status: dict) -> None:
        """A node that missed a command (e.g. its container restarted, or a
        push timed out) shows up with the wrong epoch or speed in its status
        report: re-send it what it missed. Rate-limited per node."""
        now = time.time()
        if self.session is None:
            return  # not serving (e.g. under test): nobody to push to
        if node_id not in self.nodes_by_id or now - self._last_push.get(node_id, 0) < 3:
            return
        if status.get("epoch") != self.epoch:
            if now - self.reset_at < 3:
                return  # probably a status sent just before the reset landed
            endpoint, body = "reset", self._reset_body(node_id)
        elif status.get("paused") != self.speed["paused"] or status.get("tick_ms") != self.speed["tick_ms"]:
            endpoint, body = "set_speed", dict(self.speed)
        else:
            return
        self._last_push[node_id] = now
        logger.info("re-syncing %s with %s %s", node_id, endpoint, body)
        asyncio.create_task(self.send_to(node_id, endpoint, body))

    # -- derived / display-only stats -----------------------------------
    def cure_progress(self) -> float:
        return min(1.0, len(self.successful_run_ids) / self.cure_runs_for_100)

    def median_tick(self) -> int:
        ticks = sorted(st.get("tick", 0) for st in self.node_status.values())
        return ticks[len(ticks) // 2] if ticks else 0

    def _sample_series(self) -> dict | None:
        """Add a point to the infection curve when the network's (median)
        clock has moved on. Returns the new point, if any."""
        tick = self.median_tick()
        if self.series and tick <= self.series[-1]["tick"]:
            return None
        states = [v["state"] for v in self.node_states.values()]
        point = {"tick": tick, **{s: states.count(s) for s in STATES}}
        self.series.append(point)
        return point

    def game_status(self) -> dict:
        states = [v["state"] for v in self.node_states.values()]
        alive = sum(1 for s in states if s != "DEAD")
        overt = sum(1 for s in states if s == "OVERT")
        non_dead_non_overt = sum(1 for s in states if s not in ("DEAD", "OVERT"))
        incubating = sum(1 for s in states if s == "INCUBATING")
        median_tick = self.median_tick()
        # end conditions only make sense once the game has actually started
        total_loss = self.started and alive > 0 and non_dead_non_overt == 0
        # won only when no infection is left anywhere, hidden ones included
        stable = self.started and overt == 0 and incubating == 0
        # spec's tick budget: ends stalemates (e.g. a healthy node cut off
        # from the remaining zombies by dead nodes, with no snipes left)
        time_up = self.started and median_tick >= self.max_ticks
        return {
            "alive": alive,
            "total": len(states),
            "state_counts": {s: states.count(s) for s in STATES},
            "runs_started": self.runs_started,
            "runs_completed": len(self.successful_run_ids),
            "runs_failed": self.failed_run_count,
            "runs_needed": self.cure_runs_for_100,
            "speed": self.speed,
            "started": self.started,
            "epoch": self.epoch,
            "game": self.game_number,
            "patient_zero": self.patient_zero,
            "score": alive,
            "overt": overt,
            "loss": total_loss,
            "stable": stable,
            "time_up": time_up,
            "tick": median_tick,
            "max_ticks": self.max_ticks,
            "over": self.over,
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
            "series": self.series,
            "game_status": self.game_status(),
            "debug": self.debug_view(),
        }

    def debug_view(self) -> dict:
        return {
            "node_status": self.node_status,
            "infected_by": self.infected_by,
            "death_points": self.death_point_counts,
            "anomalies_about": self.anomalies_about,
            "alerts_about": self.alerts_about,
            "run_outcomes": self.run_outcomes,
        }

    # -- event ingestion --------------------------------------------------
    async def ingest(self, event: dict) -> None:
        etype = event.get("type")
        node_id = event.get("node")

        if etype == "node_status":
            self._reconcile(node_id, event)
        if event.get("epoch") != self.epoch:
            return  # from a previous game, or from a node not reset yet

        if etype == "node_status":
            # Periodic full dump from a node: kept as "latest known", not
            # logged, otherwise it would drown every other event.
            async with self._lock:
                self.node_status[node_id] = event
                state = event.get("state")
                if node_id in self.node_states and self.node_states[node_id]["state"] != state:
                    # a state_change event got lost on the way; trust the node
                    self.node_states[node_id] = {"state": state, "ts": event.get("ts")}
                point = self._sample_series()
            await self._check_game_over()
            counters = {k: v for k, v in self.debug_view().items() if k != "node_status"}
            await self._broadcast({"type": "status", "status": event, "debug": counters,
                                   "series_point": point, "game_status": self.game_status()})
            return

        async with self._lock:
            self.event_log.append(event)

            if etype == "state_change" and node_id:
                self.node_states[node_id] = {"state": event.get("to"), "ts": event.get("ts")}
                if event.get("to") == "INCUBATING":
                    self.infected_by[node_id] = {
                        "by": event.get("by"), "tier": event.get("tier"),
                        "cause": event.get("cause"), "tick": event.get("tick"),
                        "ts": event.get("ts"),
                    }
            elif etype == "cure_run_started":
                self.runs_started += 1
            elif etype == "cure_run_result":
                outcome = "completed" if event.get("success") else event.get("reason", "failed")
                self.run_outcomes[outcome] = self.run_outcomes.get(outcome, 0) + 1
                if event.get("success"):
                    self.successful_run_ids.add(event.get("run_id"))
                else:
                    self.failed_run_count += 1
                    died_at = event.get("died_at")
                    if died_at:
                        self.death_point_counts[died_at] = self.death_point_counts.get(died_at, 0) + 1
            elif etype == "heartbeat_anomaly":
                about = event.get("about")
                self.anomalies_about[about] = self.anomalies_about.get(about, 0) + 1
            elif etype in ("alert_raised", "alert_seen"):
                self.alert_feed.append(event)
                if etype == "alert_raised":
                    about = event.get("about")
                    self.alerts_about[about] = self.alerts_about.get(about, 0) + 1

        await self._check_game_over()
        await self._broadcast({"type": "event", "event": event, "game_status": self.game_status()})

    async def _check_game_over(self) -> None:
        """Once an end condition is observed, freeze the run: pause every
        node so the final state stays put (and stops costing CPU)."""
        if self.over or not self.started:
            return
        g = self.game_status()
        reason = "loss" if g["loss"] else "stable" if g["stable"] else "time_up" if g["time_up"] else None
        if reason is None:
            return
        self.over = reason
        self.speed["paused"] = True
        logger.info("game %s over: %s, score %s/%s", self.epoch, reason, g["score"], g["total"])
        asyncio.create_task(self.send_to_all("set_speed", lambda _nid: {"paused": True}))

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


@routes.get("/game/summary")
async def get_game_summary(request: web.Request) -> web.Response:
    """Everything the end screen needs: the infection curve and the reveal
    of who was secretly infected, by whom, and when."""
    agg: Aggregator = request.app["agg"]
    return web.json_response({
        "game_status": agg.game_status(),
        "series": agg.series,
        "infected_by": agg.infected_by,
        "patient_zero": agg.patient_zero,
    })


@routes.get("/debug/events")
async def get_event_log(request: web.Request) -> web.Response:
    """Full in-memory event log (plus derived debug state), for saving a
    run and inspecting it afterwards."""
    agg: Aggregator = request.app["agg"]
    return web.json_response({
        "topology": agg.topology,
        "game_status": agg.game_status(),
        "debug": agg.debug_view(),
        "events": list(agg.event_log),
    }, headers={"Content-Disposition": "attachment; filename=contagion-run.json"})


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
    if not agg.started or agg.over:
        return web.json_response({"ok": False, "error": "game over" if agg.over else "game not started"}, status=409)
    if agg.snipe_remaining <= 0:
        return web.json_response({"ok": False, "error": "snipe budget exhausted"}, status=429)
    resp = await _forward_control(request, "snipe")
    if resp.status == 200:
        agg.snipe_remaining -= 1
    return resp


@routes.post("/control/{node_id}/cure")
async def control_cure(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    if not agg.started or agg.over:
        return web.json_response({"ok": False, "error": "game over" if agg.over else "game not started"}, status=409)
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
    try:
        body = await request.json()
        value = float(body["value"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "expected {'value': float}"}, status=400)

    await agg.send_to_all("set_trust_threshold", lambda _nid: {"value": value})
    return web.json_response({"ok": True, "threshold": value})


@routes.post("/control/speed")
async def control_speed_all(request: web.Request) -> web.Response:
    """Pause/resume and change every node's tick length. Nodes are told
    one by one, so they don't stop on exactly the same tick -- there is
    no global clock to stop. The first resume after a reset starts the game."""
    agg: Aggregator = request.app["agg"]
    try:
        body = await request.json()
        update = {}
        if "tick_ms" in body:
            update["tick_ms"] = max(50, min(20000, int(body["tick_ms"])))
        if "paused" in body:
            update["paused"] = bool(body["paused"])
    except (ValueError, TypeError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "expected {'tick_ms': int, 'paused': bool}"}, status=400)
    if agg.over and update.get("paused") is False:
        return web.json_response({"ok": False, "error": "game over, restart to play again"}, status=409)
    agg.speed.update(update)
    if update.get("paused") is False:
        agg.started = True
    await agg.send_to_all("set_speed", lambda _nid: update)
    return web.json_response({"ok": True, **agg.speed, "started": agg.started})


@routes.post("/control/restart")
async def control_restart(request: web.Request) -> web.Response:
    agg: Aggregator = request.app["agg"]
    await agg.new_game()
    return web.json_response({"ok": True, "epoch": agg.epoch, "patient_zero": agg.patient_zero})


def build_app(topology: dict, dashboard_dir: str, cure_runs_for_100: int,
              snipe_budget: int, cure_budget: int) -> web.Application:
    app = web.Application()
    app["agg"] = Aggregator(topology, cure_runs_for_100, snipe_budget, cure_budget)
    app.add_routes(routes)
    if os.path.isdir(dashboard_dir):
        async def index(_request: web.Request) -> web.FileResponse:
            return web.FileResponse(os.path.join(dashboard_dir, "index.html"))
        app.router.add_get("/", index)
        app.router.add_static("/", dashboard_dir, name="dashboard")

    async def _session_ctx(app):
        app["session"] = aiohttp.ClientSession()
        app["agg"].session = app["session"]
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
                         default=int(os.environ.get("CURE_RUNS_FOR_100", 30)))
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
