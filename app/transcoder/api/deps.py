"""Accès à l'état applicatif (capabilities, queue, scheduler) depuis les routers."""

from __future__ import annotations

from fastapi import Request

from transcoder.core.capabilities import Capabilities
from transcoder.core.queue import QueueManager
from transcoder.scan_service import ScanScheduler


def get_capabilities(request: Request) -> Capabilities:
    caps: Capabilities = request.app.state.capabilities
    return caps


def get_queue_manager(request: Request) -> QueueManager:
    qm: QueueManager = request.app.state.queue_manager
    return qm


def get_scan_scheduler(request: Request) -> ScanScheduler:
    sched: ScanScheduler = request.app.state.scan_scheduler
    return sched
