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
        web.get("/control/health", handle_health),
    ])
    return app


async def handle_health(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    return web.json_response({"node": node.cfg.node_id, "state": node.state.state.value})


async def handle_snipe(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    was_correct, new_state = node.state.snipe()
    await node.events.emit("player_action", action="snipe", correct=was_correct,
                            new_state=new_state.value)
    await node.events.emit("state_change", from_state=None, to=new_state.value, cause="snipe")
    return web.json_response({"ok": True, "correct": was_correct, "state": new_state.value})


async def handle_cure(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    was_correct, new_state = node.state.cure()
    await node.events.emit("player_action", action="cure", correct=was_correct,
                            new_state=new_state.value)
    await node.events.emit("state_change", from_state=None, to=new_state.value, cause="cure")
    return web.json_response({"ok": True, "correct": was_correct, "state": new_state.value})


async def handle_set_trust_threshold(request: web.Request) -> web.Response:
    node: "Node" = request.app["node"]
    try:
        body = await request.json()
        value = float(body["value"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"ok": False, "error": "expected {'value': float}"}, status=400)
    node.trust.set_threshold(value)
    await node.events.emit("trust_threshold_set", value=node.trust.threshold)
    return web.json_response({"ok": True, "threshold": node.trust.threshold})
