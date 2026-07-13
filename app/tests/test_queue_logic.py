"""Logique pure de la queue : backoff, bus d'événements, config runtime."""

from __future__ import annotations

import asyncio

from transcoder.core.queue import retry_backoff_s
from transcoder.events import EventBus
from transcoder.runtime_config import config_from_values


def test_retry_backoff_progression() -> None:
    assert retry_backoff_s(1) == 300.0
    assert retry_backoff_s(2) == 600.0
    assert retry_backoff_s(3) == 1200.0
    assert retry_backoff_s(0) == 300.0  # jamais négatif


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------
async def test_event_bus_queue_channel() -> None:
    bus = EventBus()
    q1 = bus.subscribe_queue()
    q2 = bus.subscribe_queue()
    bus.publish_queue({"type": "x"})
    assert (await q1.get())["type"] == "x"
    assert (await q2.get())["type"] == "x"
    bus.unsubscribe_queue(q1)
    bus.publish_queue({"type": "y"})
    assert q1.empty()
    assert (await q2.get())["type"] == "y"


async def test_event_bus_log_channels_are_isolated() -> None:
    bus = EventBus()
    qa = bus.subscribe_log(1)
    qb = bus.subscribe_log(2)
    bus.publish_log(1, {"line": "a"})
    assert (await qa.get())["line"] == "a"
    assert qb.empty()
    bus.unsubscribe_log(1, qa)
    bus.publish_log(1, {"line": "b"})  # plus d'abonné : no-op


async def test_event_bus_slow_consumer_drops_oldest() -> None:
    bus = EventBus()
    q = bus.subscribe_queue()
    for i in range(500):  # dépasse la taille max (256)
        bus.publish_queue({"i": i})
    # Le plus récent est conservé, les plus anciens jetés
    items = []
    while not q.empty():
        items.append(q.get_nowait()["i"])
    assert len(items) <= 256
    assert items[-1] == 499


# ---------------------------------------------------------------------------
# RuntimeConfig
# ---------------------------------------------------------------------------
def test_config_from_values_nominal() -> None:
    cfg = config_from_values(
        {
            "ffmpeg_path": "/opt/ffmpeg",
            "dry_run_global": False,
            "max_concurrent_jobs": 3,
            "dv_ssim_threshold": 0.95,
            "min_free_disk_gb": 50,
            "jellyfin_url": "http://jf:8096",
            "jellyfin_api_key": "k",
            "validation_decode": "sample",
        }
    )
    assert cfg.ffmpeg_path == "/opt/ffmpeg"
    assert cfg.dry_run_global is False
    assert cfg.max_concurrent_jobs == 3
    assert cfg.dv_ssim_threshold == 0.95
    assert cfg.min_free_disk_gb == 50.0
    assert cfg.jellyfin_url == "http://jf:8096"
    assert cfg.validation_decode == "sample"


def test_config_from_values_defaults_and_bad_types() -> None:
    cfg = config_from_values(
        {
            "max_concurrent_jobs": "trois",  # mauvais type → défaut
            "dry_run_global": "oui",  # mauvais type → défaut (True, sûr)
            "jellyfin_url": None,
        }
    )
    assert cfg.max_concurrent_jobs == 1
    assert cfg.dry_run_global is True  # le défaut sûr est TOUJOURS dry-run
    assert cfg.jellyfin_url is None
    assert cfg.dv_ssim_threshold == 0.90
    assert cfg.validation_decode == "full"


def _run(coro: object) -> None:  # garde asyncio importé utile en cas d'ajout
    assert asyncio is not None
