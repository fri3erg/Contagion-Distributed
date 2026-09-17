"""Data-plane transports: UDP heartbeat gossip and TCP high-trust sync
(Spec sec. 3).

Two independent transports, each with its own framing:

- UDP datagrams for heartbeat ping/ack. Also used to carry cure-run and
  alert messages between neighbor pairs currently *below* the trust
  threshold (i.e. the low-trust tier), matching-loss included.
- TCP, one newline-delimited JSON message per connection, for high-trust
  sync. Also used to carry cure-run and alert messages between pairs
  currently *at or above* the trust threshold, for more reliable delivery.

Message type dispatch is transport-agnostic (a "cure_run" message is
handled the same way whether it arrived over UDP or TCP); only the choice
of *outgoing* transport depends on the local trust score for that
neighbor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from . import alerts, cure_relay, infection

if TYPE_CHECKING:  # pragma: no cover
    from .main import Node

logger = logging.getLogger("gossip")


def _checksum(node_id: str, nonce: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{node_id}:{nonce}").hex[:8]


class HeartbeatProtocol(asyncio.DatagramProtocol):
    """UDP endpoint: heartbeat ping/ack plus low-trust-tier carrier."""

    def __init__(self, node: "Node"):
        self.node = node
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport
        self.node.udp_transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return  # a truly garbled packet on the wire; just drop it
        asyncio.create_task(dispatch_message(self.node, msg, "udp"))

    def error_received(self, exc: Exception) -> None:
        logger.debug("udp error: %s", exc)


async def dispatch_message(node: "Node", msg: dict, transport: str, writer=None) -> dict | None:
    """Route an inbound message by type. Returns a response dict for
    request/response message types (used by the TCP path); UDP-only types
    return None since UDP has no synchronous reply channel here."""
    mtype = msg.get("type")
    if mtype == "heartbeat":
        return await _handle_heartbeat_request(node, msg)
    if mtype == "heartbeat_ack":
        _handle_heartbeat_ack(node, msg)
        return None
    if mtype == "trust_sync":
        return await _handle_trust_sync(node, msg)
    if mtype == "cure_run":
        await cure_relay.handle_cure_run(node, msg)
        return {"type": "ack"}
    if mtype == "alert":
        await alerts.handle_alert(node, msg)
        return {"type": "ack"}
    logger.debug("unknown message type: %s", mtype)
    return None


# -- heartbeat ---------------------------------------------------------

async def send_heartbeat(node: "Node", neighbor: str) -> None:
    if not node.state.is_alive or node.udp_transport is None:
        return
    nonce = uuid.uuid4().hex[:12]
    msg = {"type": "heartbeat", "from": node.cfg.node_id, "ts": time.time(), "nonce": nonce}
    if infection.roll_exposure(node.state, node.cfg, node.rng, "heartbeat"):
        msg["_exposure"] = True
    node.pending_heartbeats[nonce] = {
        "neighbor": neighbor,
        "sent_at": time.time(),
        "deadline": time.time() + node.cfg.heartbeat_ack_timeout_s,
    }
    _udp_send(node, neighbor, msg)


async def _handle_heartbeat_request(node: "Node", msg: dict) -> None:
    sender = msg.get("from")
    if not node.state.is_alive or sender is None:
        return
    nonce = msg.get("nonce")
    infection.maybe_receive_exposure(node.state, msg, node.tick)

    if node.state.answers_heartbeat_normally:
        ack = {
            "type": "heartbeat_ack", "from": node.cfg.node_id, "ts": msg.get("ts"),
            "nonce": nonce, "status": "ok", "chk": _checksum(node.cfg.node_id, nonce or ""),
        }
        if infection.roll_exposure(node.state, node.cfg, node.rng, "heartbeat"):
            ack["_exposure"] = True
        _udp_send(node, sender, ack)
        node.trust.bump(sender, node.cfg.trust_increment_heartbeat)
        return

    # OVERT: detectable on this channel -- missed, late, or garbled.
    behavior = node.rng.random()
    if behavior < 1 / 3:
        return  # silently drop
    if behavior < 2 / 3:
        delay = node.rng.uniform(1.0, 2.5)
        await asyncio.sleep(delay)
        ack = {"type": "heartbeat_ack", "from": node.cfg.node_id, "ts": msg.get("ts"),
               "nonce": nonce, "status": "ok", "chk": _checksum(node.cfg.node_id, nonce or "")}
        _udp_send(node, sender, ack)
        return
    ack = {"type": "heartbeat_ack", "from": node.cfg.node_id, "ts": msg.get("ts"),
           "nonce": nonce, "status": "garbled", "chk": "0"}
    _udp_send(node, sender, ack)


def _handle_heartbeat_ack(node: "Node", msg: dict) -> None:
    nonce = msg.get("nonce")
    pending = node.pending_heartbeats.pop(nonce, None) if nonce else None
    sender = msg.get("from")
    infection.maybe_receive_exposure(node.state, msg, node.tick)

    status = msg.get("status")
    on_time = pending is not None and time.time() <= pending["deadline"]
    expected_chk = _checksum(sender or "", nonce or "")
    valid = status == "ok" and msg.get("chk") == expected_chk

    if valid and on_time:
        if sender:
            node.trust.bump(sender, node.cfg.trust_increment_heartbeat)
        return

    # Anomalous ack (garbled, or arrived after we'd already timed it out).
    if sender:
        node.suspicion.record_anomaly(sender)
        if node.rng.random() < node.cfg.alert_p_on_anomaly:
            asyncio.create_task(alerts.raise_local_alert(node, sender, "heartbeat_anomaly"))


async def check_heartbeat_timeouts(node: "Node") -> None:
    """Periodic sweep: pending pings that never got a valid on-time ack."""
    now = time.time()
    expired = [n for n, p in node.pending_heartbeats.items() if p["deadline"] < now]
    for nonce in expired:
        pending = node.pending_heartbeats.pop(nonce, None)
        if not pending:
            continue
        neighbor = pending["neighbor"]
        node.suspicion.record_anomaly(neighbor)
        if node.rng.random() < node.cfg.alert_p_on_anomaly:
            await alerts.raise_local_alert(node, neighbor, "heartbeat_anomaly")


def _udp_send(node: "Node", neighbor: str, msg: dict) -> None:
    if node.udp_transport is None:
        return
    try:
        node.udp_transport.sendto(json.dumps(msg).encode("utf-8"), (neighbor, node.cfg.udp_port))
    except OSError as exc:
        logger.debug("udp send to %s failed: %s", neighbor, exc)


# -- high-trust sync (TCP) ---------------------------------------------

async def handle_tcp_connection(node: "Node", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            return
        msg = json.loads(line.decode("utf-8"))
        resp = await dispatch_message(node, msg, "tcp")
        if resp is not None:
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
    except (asyncio.TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
        logger.debug("tcp connection error: %s", exc)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def _handle_trust_sync(node: "Node", msg: dict) -> dict:
    sender = msg.get("from")
    infection.maybe_receive_exposure(node.state, msg, node.tick)
    if sender:
        node.trust.bump(sender, node.cfg.trust_increment_sync)
    resp = {"type": "trust_sync_ack", "from": node.cfg.node_id, "payload": {"tick": node.tick}}
    if infection.roll_exposure(node.state, node.cfg, node.rng, "trust_sync"):
        resp["_exposure"] = True
    return resp


async def send_tcp_message(node: "Node", neighbor: str, msg: dict, timeout: float = 3.0) -> dict | None:
    """Open a short-lived TCP connection, send one JSON line, read the reply."""
    if not node.state.is_alive:
        return None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(neighbor, node.cfg.tcp_port), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        logger.debug("tcp connect to %s failed: %s", neighbor, exc)
        return None
    try:
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        if not line:
            return None
        return json.loads(line.decode("utf-8"))
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
        logger.debug("tcp exchange with %s failed: %s", neighbor, exc)
        return None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


async def send_trust_sync(node: "Node", neighbor: str) -> None:
    msg = {"type": "trust_sync", "from": node.cfg.node_id, "to": neighbor,
           "payload": {"tick": node.tick}}
    if infection.roll_exposure(node.state, node.cfg, node.rng, "trust_sync"):
        msg["_exposure"] = True
    resp = await send_tcp_message(node, neighbor, msg)
    if resp is not None:
        node.trust.bump(neighbor, node.cfg.trust_increment_sync)
        infection.maybe_receive_exposure(node.state, resp, node.tick)


async def route_message(node: "Node", neighbor: str, msg: dict) -> dict | None:
    """Send a cure_run/alert message to `neighbor` on whichever tier this
    node's local trust score for that neighbor currently selects."""
    if node.trust.uses_high_trust_tier(neighbor):
        return await send_tcp_message(node, neighbor, msg)
    _udp_send(node, neighbor, msg)
    return None
