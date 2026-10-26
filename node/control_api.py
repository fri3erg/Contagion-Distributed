"""Control plane: a tiny per-node HTTP endpoint (Spec sec. 9.2).

Exactly three commands, matching the spec's schema. This is deliberately
out-of-band from the data plane (separate port, plain HTTP+JSON) -- the
game-master (the aggregator's control proxy, ultimately driven by the
dashboard) calls these directly per node, the same way a real control
plane issues imperative commands to individual workers without being
part of their data path.

Player-facing policy (snipe/cure budgets and cooldowns) is *not*
enforced here: this endpoint applies whatever it's told, unconditionally.
The budget lives one layer up, in the aggregator's proxy, which is the
natural place for session/UI policy that isn't part of any node's own
game logic.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from . import gossip

if TYPE_CHECKING:  # pragma: no cover
    from .main import Node

logger = logging.getLogger("control_api")


def build_app(node: "Node") -> web.Application:
    app = web.Application()
    app["node"] = node
    app.add_routes([
        web.post("/control/snipe", handle_snipe),
        web.post("/control/cure", handle_cure),
        web.post("/control/set_trust_threshold", handle_set_trust_threshold),
        web.post("/control/set_speed", handle_set_speed),
        web.post("/control/reset", handle_reset),
        web.get("/control/health", handle_health),
    ])
    return app


async def handle_health(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    return web.json_response({"node": node.cfg.node_id, "state": node.state.state.value})


async def handle_snipe(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    prev = node.state.state.value
    was_correct, new_state = node.state.snipe()
    await node.events.emit("player_action", action="snipe", correct=was_correct,
                            prev_state=prev, new_state=new_state.value)
    await node.events.emit("state_change", from_state=prev, to=new_state.value, cause="snipe")
    node.spawn(gossip.announce_leave(node))
    return web.json_response({"ok": True, "correct": was_correct, "state": new_state.value})


async def handle_cure(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    prev = node.state.state.value
    was_correct, new_state = node.state.cure()
    await node.events.emit("player_action", action="cure", correct=was_correct,
                            prev_state=prev, new_state=new_state.value)
    await node.events.emit("state_change", from_state=prev, to=new_state.value, cause="cure")
    if new_state.value == "DEAD" and prev != "DEAD":
        node.spawn(gossip.announce_leave(node))
    return web.json_response({"ok": True, "correct": was_correct, "state": new_state.value})


async def handle_set_trust_threshold(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    try:
        body = await request.json()
        value = float(body["value"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"ok": False, "error": "expected {'value': float}"}, status=400)
    flipped = node.trust.set_threshold(value)
    await node.events.emit("trust_threshold_set", value=node.trust.threshold,
                           flipped={n: node.trust.tier(n) for n in flipped})
    return web.json_response({"ok": True, "threshold": node.trust.threshold})


async def handle_set_speed(request: web.Request) -> web.Response:
    """Debug/testing aid, not a player action: change how long a tick lasts
    and/or pause the tick loop. Message handling keeps running while
    paused, only the node's own clock stops."""
    node: "Node" = request.app["node"]
    try:
        body = await request.json()
        if "tick_ms" in body:
            node.tick_ms = max(50, min(20000, int(body["tick_ms"])))
        if "paused" in body:
            node.paused = bool(body["paused"])
    except (ValueError, TypeError):
        return web.json_response({"ok": False, "error": "expected {'tick_ms': int, 'paused': bool}"}, status=400)
    await node.events.emit("speed_set", tick_ms=node.tick_ms, paused=node.paused)
    return web.json_response({"ok": True, "tick_ms": node.tick_ms, "paused": node.paused})


async def handle_reset(request: web.Request) -> web.Response:
    """Start a new game: fresh local state under a new epoch number.
    Also carries the speed so the node comes back paused/at the right pace."""
    node: "Node" = request.app["node"]
    try:
        body = await request.json()
        epoch = int(body["epoch"])
        patient_zero = bool(body.get("patient_zero", False))
        if "tick_ms" in body:
            node.tick_ms = max(50, min(20000, int(body["tick_ms"])))
        if "paused" in body:
            node.paused = bool(body["paused"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"ok": False, "error": "expected {'epoch': int, ...}"}, status=400)
    await node.reset(epoch, patient_zero)
    return web.json_response({"ok": True, "epoch": node.epoch, "state": node.state.state.value})
