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
import socket
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
        self.node.spawn(dispatch_message(self.node, msg, "udp"))

    def error_received(self, exc: Exception) -> None:
        logger.debug("udp error: %s", exc)


async def dispatch_message(node: "Node", msg: dict, transport: str, writer=None) -> dict | None:
    """Route an inbound message by type. Returns a response dict for
    request/response message types (used by the TCP path); UDP-only types
    return None since UDP has no synchronous reply channel here."""
    mtype = msg.get("type")
    if msg.get("epoch") != node.epoch:
        return None  # leftover from another game (before/after a restart)
    if not node.state.is_alive:
        # A dead node is out of the network and takes no part in any
        # protocol. Cure runs are still reported so a run that walked into
        # a dead node doesn't silently vanish from the debug view.
        if mtype == "cure_run":
            await node.events.emit("cure_run_result", run_id=msg.get("run_id"), success=False,
                                   path=list(msg.get("path", [])) + [node.cfg.node_id],
                                   died_at=node.cfg.node_id, reason="dead_node")
        return None
    if mtype == "heartbeat":
        return await _handle_heartbeat_request(node, msg)
    if mtype == "heartbeat_ack":
        await _handle_heartbeat_ack(node, msg)
        return None
    if mtype == "trust_sync":
        return await _handle_trust_sync(node, msg)
    if mtype == "cure_run":
        await cure_relay.handle_cure_run(node, msg)
        return {"type": "ack"}
    if mtype == "alert":
        await alerts.handle_alert(node, msg)
        return {"type": "ack"}
    if mtype == "leave":
        await _handle_leave(node, msg)
        return {"type": "ack"}
    logger.debug("unknown message type: %s", mtype)
    return None


# -- heartbeat ---------------------------------------------------------

async def send_heartbeat(node: "Node", neighbor: str) -> None:
    if not node.state.is_alive or node.udp_transport is None:
        return
    nonce = uuid.uuid4().hex[:12]
    msg = {"type": "heartbeat", "from": node.cfg.node_id, "ts": time.time(), "nonce": nonce}
    _maybe_attach_exposure(node, msg, neighbor, "heartbeat")
    node.stats["heartbeats_sent"] += 1
    node.pending_heartbeats[nonce] = {
        "neighbor": neighbor,
        "sent_at": time.time(),
        "deadline": time.time() + node.cfg.heartbeat_ack_timeout_s,
    }
    await _udp_send(node, neighbor, msg)


async def _handle_heartbeat_request(node: "Node", msg: dict) -> None:
    sender = msg.get("from")
    if sender is None:
        return
    nonce = msg.get("nonce")
    await _receive_exposure(node, msg, "heartbeat")

    # The hidden infection's tell: now and then it just doesn't answer.
    # From outside this looks exactly like a lost UDP packet.
    if (node.state.state.value == "INCUBATING"
            and node.rng.random() < node.cfg.incubating_hb_fail_p):
        await node.events.emit("heartbeat_misbehave", to=sender, behavior="tell_dropped")
        return

    if node.state.answers_heartbeat_normally:
        ack = {
            "type": "heartbeat_ack", "from": node.cfg.node_id, "ts": msg.get("ts"),
            "nonce": nonce, "status": "ok", "chk": _checksum(node.cfg.node_id, nonce or ""),
        }
        # no exposure roll on the reply: each node already rolls once per
        # neighbor per tick on its own ping, which is the spec's "1-2% per
        # tick of contact". Rolling on replies too doubled the rate.
        await _udp_send(node, sender, ack)
        _bump_trust(node, sender, node.cfg.trust_increment_heartbeat)
        return

    # OVERT: detectable on this channel -- missed, late, or garbled.
    behavior = node.rng.random()
    if behavior < 1 / 3:
        await node.events.emit("heartbeat_misbehave", to=sender, behavior="dropped")
        return
    if behavior < 2 / 3:
        delay = node.rng.uniform(1.0, 2.5)
        await node.events.emit("heartbeat_misbehave", to=sender, behavior="late",
                               delay_s=round(delay, 2))
        await asyncio.sleep(delay)
        ack = {"type": "heartbeat_ack", "from": node.cfg.node_id, "ts": msg.get("ts"),
               "nonce": nonce, "status": "ok", "chk": _checksum(node.cfg.node_id, nonce or "")}
        await _udp_send(node, sender, ack)
        return
    await node.events.emit("heartbeat_misbehave", to=sender, behavior="garbled")
    ack = {"type": "heartbeat_ack", "from": node.cfg.node_id, "ts": msg.get("ts"),
           "nonce": nonce, "status": "garbled", "chk": "0"}
    await _udp_send(node, sender, ack)


async def _handle_heartbeat_ack(node: "Node", msg: dict) -> None:
    nonce = msg.get("nonce")
    pending = node.pending_heartbeats.pop(nonce, None) if nonce else None
    sender = msg.get("from")
    await _receive_exposure(node, msg, "heartbeat")

    status = msg.get("status")
    on_time = pending is not None and time.time() <= pending["deadline"]
    expected_chk = _checksum(sender or "", nonce or "")
    valid = status == "ok" and msg.get("chk") == expected_chk

    if sender and node.suspicion.record_reply(sender):
        node.spawn(node.events.emit("neighbor_up", neighbor=sender))

    if valid and on_time:
        if sender:
            node.suspicion.record_good_reply(sender)
            _bump_trust(node, sender, node.cfg.trust_increment_heartbeat)
        return

    # Anomalous ack (garbled, or arrived after we'd already timed it out).
    if sender:
        _record_anomaly(node, sender, "garbled" if not valid else "late")


def check_heartbeat_timeouts(node: "Node") -> None:
    """Periodic sweep: pending pings that never got a valid on-time ack."""
    now = time.time()
    expired = [n for n, p in node.pending_heartbeats.items() if p["deadline"] < now]
    for nonce in expired:
        pending = node.pending_heartbeats.pop(nonce, None)
        if pending:
            neighbor = pending["neighbor"]
            _record_anomaly(node, neighbor, "timeout")
            if node.suspicion.record_miss(neighbor):
                node.spawn(node.events.emit(
                    "neighbor_down", neighbor=neighbor,
                    misses=node.suspicion.DOWN_AFTER_MISSES))


def _record_anomaly(node: "Node", neighbor: str, kind: str) -> None:
    count = node.suspicion.record_anomaly(neighbor)
    if kind == "garbled":
        # a missing or late reply may just be a lost packet; a garbled one
        # is something only a zombie does, so that's proof of an outbreak
        node.learn_of_outbreak("garbled_heartbeat", neighbor)
    # trust is earned slowly and lost quickly
    if node.trust.decay(neighbor, node.cfg.trust_penalty_anomaly):
        node.spawn(node.events.emit("tier_change", neighbor=neighbor,
                                    tier=node.trust.tier(neighbor),
                                    score=round(node.trust.score(neighbor), 3),
                                    threshold=node.trust.threshold, cause="anomaly"))
    node.stats["heartbeat_anomalies"] += 1
    # One lost packet proves nothing (the cheap tier loses plenty); a node
    # only speaks up after a streak of bad replies, or a garbled one.
    raise_alert = kind == "garbled" or node.suspicion.streak(neighbor) >= node.cfg.alert_after_streak
    node.spawn(node.events.emit("heartbeat_anomaly", about=neighbor, kind=kind,
                                count=count, alerted=raise_alert))
    if raise_alert:
        node.spawn(alerts.raise_local_alert(
            node, neighbor, "garbled" if kind == "garbled" else "unresponsive"))


ADDR_TTL_S = 5.0
FAILED_LOOKUP_TTL_S = 1.0
_addr_cache: dict[str, tuple[tuple | None, float]] = {}


async def _resolve(neighbor: str, port: int) -> tuple | None:
    """Resolve a neighbor's hostname without blocking the event loop.

    sendto() with a hostname does a *blocking* DNS lookup; when a neighbor's
    container is down that lookup hangs, and with it the whole node. Results
    are cached briefly, since a restarted container may come back with a
    different IP."""
    cached = _addr_cache.get(neighbor)
    if cached:
        addr, at = cached
        if time.time() - at < (ADDR_TTL_S if addr else FAILED_LOOKUP_TTL_S):
            return addr
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            neighbor, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
        addr = infos[0][4]
    except OSError:
        addr = None  # unreachable right now; the datagram is simply lost
    _addr_cache[neighbor] = (addr, time.time())
    return addr


async def _udp_send(node: "Node", neighbor: str, msg: dict) -> None:
    if node.udp_transport is None:
        return
    # Simulated loss on the cheap tier. Docker's virtual network never drops
    # anything, so the sender rolls it: this is what makes low-trust links
    # unreliable, and what makes healthy nodes look bad now and then.
    if node.rng.random() < node.cfg.udp_loss:
        node.stats["udp_dropped"] += 1
        return
    msg.setdefault("epoch", node.epoch)
    addr = await _resolve(neighbor, node.cfg.udp_port)
    if addr is None:
        return
    try:
        node.udp_transport.sendto(json.dumps(msg).encode("utf-8"), addr)
    except OSError as exc:
        logger.debug("udp send to %s failed: %s", neighbor, exc)


# -- infection / trust bookkeeping -------------------------------------

def _maybe_attach_exposure(node: "Node", msg: dict, to: str, tier: str) -> None:
    """Sender side: roll the transmission chance for an outgoing message."""
    if infection.roll_exposure(node.state, node.cfg, node.rng, tier):
        msg["_exposure"] = True
        node.stats["exposures_sent"] += 1
        node.spawn(node.events.emit("exposure_sent", to=to, tier=tier,
                                    sender_state=node.state.state.value))


async def _receive_exposure(node: "Node", msg: dict, tier: str) -> None:
    """Receiver side: react to an exposure marker and report the outcome."""
    if not msg.get("_exposure"):
        return
    source = msg.get("from")
    state_before = node.state.state.value
    infected = infection.maybe_receive_exposure(node.state, msg, node.tick)
    node.stats["exposures_received"] += 1
    await node.events.emit("exposure", source=source, tier=tier, infected=infected,
                           state_before=state_before)
    if infected:
        await node.events.emit("state_change", from_state="HEALTHY", to="INCUBATING",
                               cause="exposure", by=source, tier=tier,
                               overt_at_tick=node.state.incubation_end_tick)


def _bump_trust(node: "Node", neighbor: str, amount: float) -> None:
    if node.trust.bump(neighbor, amount):
        node.spawn(node.events.emit("tier_change", neighbor=neighbor,
                                    tier=node.trust.tier(neighbor),
                                    score=round(node.trust.score(neighbor), 3),
                                    threshold=node.trust.threshold, cause="trust_accrual"))


# -- high-trust sync (TCP) ---------------------------------------------

async def handle_tcp_connection(node: "Node", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            return
        msg = json.loads(line.decode("utf-8"))
        resp = await dispatch_message(node, msg, "tcp")
        if resp is not None:
            resp["epoch"] = node.epoch
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
    await _receive_exposure(node, msg, "trust_sync")
    if sender:
        _bump_trust(node, sender, node.cfg.trust_increment_sync)
    resp = {"type": "trust_sync_ack", "from": node.cfg.node_id, "payload": {"tick": node.tick}}
    if sender:
        _maybe_attach_exposure(node, resp, sender, "trust_sync")
    return resp


async def send_tcp_message(node: "Node", neighbor: str, msg: dict, timeout: float = 3.0,
                           allow_dead: bool = False) -> dict | None:
    """Open a short-lived TCP connection, send one JSON line, read the reply."""
    if not node.state.is_alive and not allow_dead:
        return None
    msg.setdefault("epoch", node.epoch)
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
    _maybe_attach_exposure(node, msg, neighbor, "trust_sync")
    node.stats["trust_syncs_sent"] += 1
    resp = await send_tcp_message(node, neighbor, msg)
    if resp is not None and resp.get("epoch") == node.epoch:
        _bump_trust(node, neighbor, node.cfg.trust_increment_sync)
        await _receive_exposure(node, resp, "trust_sync")


async def route_message(node: "Node", neighbor: str, msg: dict) -> str | None:
    """Send a cure_run/alert message to `neighbor` on whichever tier this
    node's local trust score for that neighbor currently selects.

    Returns the tier used ("tcp"/"udp"), or None if a TCP delivery failed.
    UDP is fire-and-forget, so it always counts as sent."""
    if node.trust.uses_high_trust_tier(neighbor):
        resp = await send_tcp_message(node, neighbor, msg)
        return "tcp" if resp is not None else None
    await _udp_send(node, neighbor, msg)
    return "udp"


# -- graceful leave ----------------------------------------------------

async def announce_leave(node: "Node") -> None:
    """Last words of a node that has just been killed: tell each neighbor
    directly (over TCP, so it isn't lost) instead of letting them find out
    from missed heartbeats. Membership protocols call this a graceful leave."""
    msg = {"type": "leave", "from": node.cfg.node_id, "epoch": node.epoch}
    await asyncio.gather(*(send_tcp_message(node, nb, dict(msg), allow_dead=True)
                           for nb in node.cfg.peers))


async def _handle_leave(node: "Node", msg: dict) -> None:
    sender = msg.get("from")
    if not sender:
        return
    node.suspicion.mark_down(sender)
    await node.events.emit("neighbor_down", neighbor=sender, misses=0, reason="left")
    # a neighbor being shot means the outbreak is real
    node.learn_of_outbreak("neighbor_killed", sender)
    node.spawn(alerts.raise_local_alert(node, sender, "killed"))
