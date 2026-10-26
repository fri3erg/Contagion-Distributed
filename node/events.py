"""Observability-plane event emission (Spec sec. 9.3).

Fire-and-forget JSON POSTs to the aggregator. This channel is one-way and
read-only from the game's point of view: nothing the aggregator does can
ever change a node's actual state. If the aggregator is briefly
unreachable, events are dropped rather than buffered -- observability must
never become a dependency of the data plane.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import aiohttp

logger = logging.getLogger("events")


class EventEmitter:
    def __init__(self, node_id: str, aggregator_url: str, session: aiohttp.ClientSession,
                 tick_fn: Callable[[], int] | None = None,
                 epoch_fn: Callable[[], int] | None = None):
        self._node_id = node_id
        self._url = aggregator_url.rstrip("/") + "/events"
        self._session = session
        self._tick_fn = tick_fn
        self._epoch_fn = epoch_fn

    async def emit(self, event_type: str, **fields) -> None:
        payload = {"type": event_type, "node": self._node_id, "ts": time.time(), **fields}
        if self._tick_fn is not None:
            # local tick of the emitting node; ticks are not synchronized
            # across nodes, it's only meant for reading a single node's timeline
            payload.setdefault("tick", self._tick_fn())
        if self._epoch_fn is not None:
            payload.setdefault("epoch", self._epoch_fn())
        try:
            async with self._session.post(self._url, json=payload, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                await resp.release()
        except Exception as exc:  # noqa: BLE001 - observability must never crash the node
            logger.debug("event emit failed (%s): %s", event_type, exc)
