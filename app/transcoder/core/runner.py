"""Exécution d'un encodage FFmpeg : progression, logs, annulation (§5.4).

- stdout (`-progress pipe:1`) : blocs clé=valeur terminés par `progress=…`,
  parsés en ProgressUpdate (percent, fps, speed, ETA) ;
- stderr : écrit en continu dans le fichier de log ET diffusé ligne à ligne
  via callback (WebSocket /ws/jobs/{id}/log, module API) ;
- annulation : SIGTERM propre, escalade SIGKILL après 10 s (§5.2).

Le parsing est pur et testé sans FFmpeg (faux binaire Python dans les tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SIGKILL_GRACE_S = 10.0
LOG_TAIL_LINES = 200


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """Un bloc de progression FFmpeg interprété."""

    percent: float | None
    out_time_s: float | None
    fps: float | None
    speed: float | None
    bitrate_kbps: float | None
    # Taille déjà écrite par le muxer : permet de PROJETER la taille finale
    # et d'abandonner tôt un encodage sans gain (early_abort_no_gain)
    total_size_bytes: int | None
    eta_s: float | None
    finished: bool


@dataclass(frozen=True, slots=True)
class RunResult:
    """Issue d'un encodage."""

    exit_code: int
    cancelled: bool
    avg_fps: float | None
    avg_speed: float | None
    log_tail: str


# ---------------------------------------------------------------------------
# Parsing pur
# ---------------------------------------------------------------------------
def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def parse_progress_block(block: dict[str, str], duration_s: float | None) -> ProgressUpdate:
    """Interprète un bloc `-progress` (clé=valeur) en ProgressUpdate."""
    out_time_s: float | None = None
    us = block.get("out_time_us") or block.get("out_time_ms")
    if us is not None:
        v = _parse_float(us)
        if v is not None and v >= 0:
            out_time_s = v / 1_000_000.0
    elif "out_time" in block:
        # Format HH:MM:SS.micro
        parts = block["out_time"].split(":")
        if len(parts) == 3:
            h, m, s = (_parse_float(p) for p in parts)
            if h is not None and m is not None and s is not None:
                out_time_s = h * 3600 + m * 60 + s

    fps = _parse_float(block.get("fps", ""))
    speed_raw = block.get("speed", "").strip().rstrip("x")
    speed = _parse_float(speed_raw)
    bitrate_raw = block.get("bitrate", "").strip()
    bitrate = None
    if bitrate_raw.endswith("kbits/s"):
        bitrate = _parse_float(bitrate_raw[: -len("kbits/s")])
    total_size_raw = _parse_float(block.get("total_size", ""))
    total_size = int(total_size_raw) if total_size_raw and total_size_raw > 0 else None

    percent: float | None = None
    eta_s: float | None = None
    if duration_s and duration_s > 0 and out_time_s is not None:
        percent = max(0.0, min(100.0, out_time_s / duration_s * 100.0))
        if speed and speed > 0:
            eta_s = max(0.0, (duration_s - out_time_s) / speed)

    return ProgressUpdate(
        percent=percent,
        out_time_s=out_time_s,
        fps=fps if fps and fps > 0 else None,
        speed=speed if speed and speed > 0 else None,
        bitrate_kbps=bitrate,
        total_size_bytes=total_size,
        eta_s=eta_s,
        finished=block.get("progress") == "end",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
ProgressCallback = Callable[[ProgressUpdate], Awaitable[None]]
LogLineCallback = Callable[[str], Awaitable[None]]


async def _consume_progress(
    stream: asyncio.StreamReader,
    duration_s: float | None,
    on_progress: ProgressCallback | None,
    fps_samples: list[float],
    speed_samples: list[float],
) -> None:
    block: dict[str, str] = {}
    while True:
        raw = await stream.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        block[key] = value
        if key == "progress":
            update = parse_progress_block(block, duration_s)
            if update.fps is not None:
                fps_samples.append(update.fps)
            if update.speed is not None:
                speed_samples.append(update.speed)
            if on_progress is not None:
                await on_progress(update)
            block = {}


async def _consume_stderr(
    stream: asyncio.StreamReader,
    log_path: str,
    on_log_line: LogLineCallback | None,
    tail: deque[str],
) -> None:
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log_file:
        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\n")
            log_file.write(line + "\n")
            log_file.flush()
            tail.append(line)
            if on_log_line is not None:
                await on_log_line(line)


async def run_ffmpeg(
    cmd: list[str],
    *,
    duration_s: float | None,
    log_path: str,
    on_progress: ProgressCallback | None = None,
    on_log_line: LogLineCallback | None = None,
    cancel_event: asyncio.Event | None = None,
) -> RunResult:
    """Lance FFmpeg et suit progression/logs jusqu'à la fin ou l'annulation."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
    )
    assert proc.stdout is not None and proc.stderr is not None

    fps_samples: list[float] = []
    speed_samples: list[float] = []
    tail: deque[str] = deque(maxlen=LOG_TAIL_LINES)

    consumers = asyncio.gather(
        _consume_progress(proc.stdout, duration_s, on_progress, fps_samples, speed_samples),
        _consume_stderr(proc.stderr, log_path, on_log_line, tail),
    )

    cancelled = False

    async def _watch_cancel() -> None:
        nonlocal cancelled
        if cancel_event is None:
            return
        await cancel_event.wait()
        cancelled = True
        logger.info("Annulation demandée : SIGTERM au processus FFmpeg (pid %s)", proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_SIGKILL_GRACE_S)
        except asyncio.TimeoutError:
            logger.warning("FFmpeg ne répond pas au SIGTERM : SIGKILL")
            with contextlib.suppress(ProcessLookupError):
                proc.kill()

    watcher = asyncio.create_task(_watch_cancel())
    try:
        await proc.wait()
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        with contextlib.suppress(Exception):
            await consumers

    def _avg(samples: list[float]) -> float | None:
        return round(sum(samples) / len(samples), 2) if samples else None

    return RunResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        cancelled=cancelled,
        avg_fps=_avg(fps_samples),
        avg_speed=_avg(speed_samples),
        log_tail="\n".join(tail),
    )
