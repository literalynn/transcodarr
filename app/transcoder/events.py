"""Bus d'événements en mémoire pour les WebSockets (§5.4).

- canal « queue » : événements d'état des jobs + progression agrégée
  (`/ws/queue`) ;
- canaux « log » par job : lignes stderr FFmpeg + progression
  (`/ws/jobs/{id}/log`).

Les abonnés sont des asyncio.Queue bornées : un client lent perd des
messages (drop silencieux) plutôt que de bloquer les workers.
"""

from __future__ import annotations

import asyncio
from typing import Any

_QUEUE_SIZE = 256


class EventBus:
    def __init__(self) -> None:
        self._queue_subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._log_subs: dict[int, set[asyncio.Queue[dict[str, Any]]]] = {}

    # --- Canal queue --------------------------------------------------------
    def subscribe_queue(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._queue_subs.add(q)
        return q

    def unsubscribe_queue(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._queue_subs.discard(q)

    def publish_queue(self, event: dict[str, Any]) -> None:
        for q in list(self._queue_subs):
            _offer(q, event)

    # --- Canaux log par job ---------------------------------------------------
    def subscribe_log(self, job_id: int) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._log_subs.setdefault(job_id, set()).add(q)
        return q

    def unsubscribe_log(self, job_id: int, q: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._log_subs.get(job_id)
        if subs is not None:
            subs.discard(q)
            if not subs:
                del self._log_subs[job_id]

    def publish_log(self, job_id: int, event: dict[str, Any]) -> None:
        for q in list(self._log_subs.get(job_id, ())):
            _offer(q, event)


def _offer(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
    """Dépose sans bloquer ; en cas de saturation, jette le plus ancien."""
    try:
        q.put_nowait(event)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(event)
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            pass


# Instance globale de l'application (un seul process)
event_bus = EventBus()
