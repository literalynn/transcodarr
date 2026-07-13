"""Mesure des ressources : app (cgroup v2 du conteneur) vs hôte (/proc).

Dans un conteneur, /proc/stat et /proc/meminfo exposent les compteurs de
l'HÔTE entier, tandis que /sys/fs/cgroup/… (cgroup v2, vue namespacée)
expose la consommation du conteneur seul — exactement la séparation
« app vs reste du système » voulue par le dashboard.

GPU Intel : best-effort via `intel_gpu_top -J` (nécessite CAP_PERFMON,
ajouté dans le compose) ; à défaut, fréquence courante/max en proxy.
Tout est dégradable : champ à null si la source n'existe pas (dev macOS).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_PROC_STAT = "/proc/stat"
_PROC_MEMINFO = "/proc/meminfo"
_CGROUP_CPU = "/sys/fs/cgroup/cpu.stat"
_CGROUP_MEM = "/sys/fs/cgroup/memory.current"

# Échantillons précédents pour les deltas CPU (module-level : un par process)
_prev_host: tuple[float, int, int] | None = None  # (ts, busy_jiffies, total_jiffies)
_prev_app: tuple[float, int] | None = None  # (ts, usage_usec)


def parse_proc_stat_line(line: str) -> tuple[int, int] | None:
    """Ligne agrégée `cpu  ...` de /proc/stat → (busy, total) en jiffies."""
    parts = line.split()
    if len(parts) < 8 or parts[0] != "cpu":
        return None
    try:
        values = [int(v) for v in parts[1:11]]
    except ValueError:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    total = sum(values)
    return total - idle, total


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """/proc/meminfo → (total_bytes, available_bytes)."""
    total = available = None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) * 1024
        elif line.startswith("MemAvailable:"):
            available = int(line.split()[1]) * 1024
    if total is None or available is None:
        return None
    return total, available


def parse_cgroup_cpu_usage_usec(text: str) -> int | None:
    for line in text.splitlines():
        if line.startswith("usage_usec"):
            with contextlib.suppress(ValueError, IndexError):
                return int(line.split()[1])
    return None


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _host_cpu_percent() -> float | None:
    """% CPU occupé de l'HÔTE entier depuis le dernier appel."""
    global _prev_host
    raw = _read(_PROC_STAT)
    if raw is None:
        return None
    parsed = parse_proc_stat_line(raw.splitlines()[0])
    if parsed is None:
        return None
    busy, total = parsed
    now = time.monotonic()
    prev = _prev_host
    _prev_host = (now, busy, total)
    if prev is None or total <= prev[2]:
        return None
    return min(100.0, (busy - prev[1]) / (total - prev[2]) * 100.0)


def _app_cpu_percent(cores: int) -> float | None:
    """% CPU (de l'hôte entier) consommé par le CONTENEUR seul."""
    global _prev_app
    raw = _read(_CGROUP_CPU)
    if raw is None:
        return None
    usage = parse_cgroup_cpu_usage_usec(raw)
    if usage is None:
        return None
    now = time.monotonic()
    prev = _prev_app
    _prev_app = (now, usage)
    if prev is None or now <= prev[0]:
        return None
    elapsed_usec = (now - prev[0]) * 1_000_000 * max(1, cores)
    return min(100.0, (usage - prev[1]) / elapsed_usec * 100.0)


async def _gpu_busy_percent(drm_node: str) -> dict[str, Any]:
    """Utilisation GPU : intel_gpu_top -J (1 échantillon), sinon fréquences."""
    out: dict[str, Any] = {"busy_percent": None, "freq_mhz": None, "freq_max_mhz": None}
    # Fréquences sysfs (toujours lisibles avec le groupe render)
    card = os.path.basename(drm_node).replace("renderD", "")
    for candidate in ("/sys/class/drm/card0", "/sys/class/drm/card1"):
        cur = _read(f"{candidate}/gt_cur_freq_mhz")
        maxi = _read(f"{candidate}/gt_max_freq_mhz")
        if cur and maxi:
            with contextlib.suppress(ValueError):
                out["freq_mhz"] = int(cur.strip())
                out["freq_max_mhz"] = int(maxi.strip())
            break
    _ = card
    # intel_gpu_top : moyenne d'occupation des moteurs (500 ms d'échantillon)
    try:
        proc = await asyncio.create_subprocess_exec(
            "intel_gpu_top", "-J", "-s", "500", "-d", f"drm:{drm_node}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        buffer = b""
        try:
            async with asyncio.timeout(3.0):
                # Le flux est un tableau JSON streamé : on lit jusqu'à la fin
                # du premier objet complet
                depth = 0
                started = False
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    for i, ch in enumerate(buffer):
                        c = chr(ch)
                        if c == "{":
                            depth += 1
                            started = True
                        elif c == "}":
                            depth -= 1
                            if started and depth == 0:
                                raise StopAsyncIteration(i)
        except StopAsyncIteration as stop:
            end = int(str(stop)) + 1
            start = buffer.index(b"{")
            sample: Any = json.loads(buffer[start:end])
            engines = sample.get("engines", {}) if isinstance(sample, dict) else {}
            busies = [
                float(e.get("busy", 0.0))
                for e in engines.values()
                if isinstance(e, dict)
            ]
            if busies:
                out["busy_percent"] = round(max(busies), 1)
        except (asyncio.TimeoutError, json.JSONDecodeError, ValueError):
            pass
        finally:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    except (OSError, FileNotFoundError):
        pass  # intel_gpu_top absent ou sans CAP_PERFMON : fréquences seules
    return out


async def sample_resources(drm_node: str = "/dev/dri/renderD128") -> dict[str, Any]:
    """Instantané CPU/RAM/GPU, app vs hôte. Champs null si indisponibles."""
    cores = os.cpu_count() or 1
    host_cpu = _host_cpu_percent()
    app_cpu = _app_cpu_percent(cores)

    mem_total = mem_available = app_mem = None
    meminfo = _read(_PROC_MEMINFO)
    if meminfo is not None:
        parsed = parse_meminfo(meminfo)
        if parsed is not None:
            mem_total, mem_available = parsed
    mem_current = _read(_CGROUP_MEM)
    if mem_current is not None:
        with contextlib.suppress(ValueError):
            app_mem = int(mem_current.strip())

    gpu = await _gpu_busy_percent(drm_node)

    other_cpu = None
    if host_cpu is not None and app_cpu is not None:
        other_cpu = max(0.0, host_cpu - app_cpu)
    mem_used = (
        mem_total - mem_available if mem_total is not None and mem_available is not None else None
    )
    other_mem = (
        max(0, mem_used - app_mem) if mem_used is not None and app_mem is not None else None
    )
    return {
        "cpu": {
            "cores": cores,
            "app_percent": round(app_cpu, 1) if app_cpu is not None else None,
            "other_percent": round(other_cpu, 1) if other_cpu is not None else None,
            "host_percent": round(host_cpu, 1) if host_cpu is not None else None,
        },
        "memory": {
            "total_bytes": mem_total,
            "app_bytes": app_mem,
            "other_bytes": other_mem,
            "used_bytes": mem_used,
        },
        "gpu": gpu,
    }
