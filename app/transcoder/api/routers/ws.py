"""WebSockets (§5.4) : /ws/queue et /ws/jobs/{id}/log."""

from __future__ import annotations

import asyncio
import contextlib
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from transcoder.core.runner import LOG_TAIL_LINES
from transcoder.db import session_scope
from transcoder.events import event_bus
from transcoder.models import Job

router = APIRouter()

_PING_INTERVAL_S = 30.0


@router.websocket("/ws/queue")
async def ws_queue(websocket: WebSocket) -> None:
    """Événements d'état des jobs + progression agrégée."""
    await websocket.accept()
    queue = event_bus.subscribe_queue()
    qm = websocket.app.state.queue_manager
    try:
        await websocket.send_json({"type": "snapshot", "active": qm.active_jobs()})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL_S)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        event_bus.unsubscribe_queue(queue)


@router.websocket("/ws/jobs/{job_id}/log")
async def ws_job_log(websocket: WebSocket, job_id: int) -> None:
    """Log FFmpeg ligne à ligne + progression pour un job."""
    await websocket.accept()
    queue = event_bus.subscribe_log(job_id)
    try:
        # Rattrapage : fin du log existant (fichier, sinon log_tail en base)
        backlog: list[str] = []
        async with session_scope() as session:
            job = await session.get(Job, job_id)
            if job is not None:
                if job.log_path and os.path.exists(job.log_path):
                    with open(job.log_path, encoding="utf-8", errors="replace") as fh:
                        backlog = fh.read().splitlines()[-LOG_TAIL_LINES:]
                elif job.log_tail:
                    backlog = job.log_tail.splitlines()
        await websocket.send_json({"type": "backlog", "lines": backlog})

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL_S)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
                continue
            await websocket.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with contextlib.suppress(Exception):
            event_bus.unsubscribe_log(job_id, queue)
