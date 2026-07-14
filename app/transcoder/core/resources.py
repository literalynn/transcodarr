"""Mesure des ressources : app (cgroup v2 du conteneur) vs hôte (/proc).

Dans un conteneur, /proc/stat et /proc/meminfo exposent les compteurs de
l'HÔTE entier, tandis que /sys/fs/cgroup/… (cgroup v2, vue namespacée)
expose la consommation du conteneur seul — exactement la séparation
« app vs reste du système » voulue par le dashboard.

GPU : lecture des compteurs DRM `/proc/<pid>/fdinfo` (drm-engine-*) des
processus du conteneur — c'est l'usage GPU de l'APP (ffmpeg), sans aucun
privilège requis (intel_gpu_top exige des capacités effectives root,
inaccessibles à un processus non-root même avec cap_add). Fréquences
sysfs en complément. Tout est dégradable : champ à null si la source
n'existe pas (dev macOS).
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_PROC_STAT = "/proc/stat"
_PROC_MEMINFO = "/proc/meminfo"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _cgroup_base() -> str:
    """Répertoire cgroup v2 du PROCESSUS (suivi de /proc/self/cgroup).

    En namespace cgroup privé (défaut docker/cgroup v2), la racine
    /sys/fs/cgroup est déjà celle du conteneur. En cgroupns=host, elle
    est celle de l'HÔTE entier : memory.current y vaudrait toute la RAM
    du système et la séparation app/système serait fausse — d'où le
    suivi du chemin réel indiqué par /proc/self/cgroup."""
    raw = _read("/proc/self/cgroup") or ""
    for line in raw.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
            if rel and rel != "/":
                candidate = "/sys/fs/cgroup" + rel
                if os.path.exists(f"{candidate}/memory.current"):
                    return candidate
    return "/sys/fs/cgroup"


_CGROUP_BASE = _cgroup_base()
_CGROUP_CPU = f"{_CGROUP_BASE}/cpu.stat"
_CGROUP_MEM = f"{_CGROUP_BASE}/memory.current"

# Échantillons précédents pour les deltas (module-level : un par process)
_prev_host: tuple[float, int, int] | None = None  # (ts, busy_jiffies, total_jiffies)
_prev_app: tuple[float, int] | None = None  # (ts, usage_usec)
_prev_gpu: tuple[float, dict[str, int]] | None = None  # (ts, ns cumulés par moteur)


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


def parse_drm_fdinfo(text: str) -> tuple[str | None, dict[str, int]]:
    """Un fichier fdinfo DRM → (client_id, {moteur: ns cumulés}).

    Format i915/xe : `drm-client-id: 42`, `drm-engine-render: 123456 ns`,
    `drm-engine-video: … ns`. Un même client apparaît sur plusieurs fd :
    l'appelant déduplique par client_id."""
    client_id: str | None = None
    engines: dict[str, int] = {}
    for line in text.splitlines():
        if line.startswith("drm-client-id:"):
            client_id = line.split(":", 1)[1].strip()
        elif line.startswith("drm-engine-"):
            key, _, value = line.partition(":")
            engine = key[len("drm-engine-") :]
            parts = value.split()
            if parts:
                with contextlib.suppress(ValueError):
                    engines[engine] = engines.get(engine, 0) + int(parts[0])
    return client_id, engines


def _sample_drm_engines() -> dict[str, int]:
    """Somme des ns d'occupation par moteur GPU, tous processus du conteneur.

    Le namespace PID du conteneur limite naturellement /proc à NOS processus
    (python + ffmpeg) : c'est donc bien l'usage GPU de l'app qu'on mesure."""
    totals: dict[str, int] = {}
    seen_clients: set[str] = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return totals
    for pid in pids:
        fdinfo_dir = f"/proc/{pid}/fdinfo"
        try:
            fds = os.listdir(fdinfo_dir)
        except OSError:
            continue
        for fd in fds:
            text = _read(f"{fdinfo_dir}/{fd}")
            if not text or "drm-engine-" not in text:
                continue
            client_id, engines = parse_drm_fdinfo(text)
            dedupe_key = client_id or f"{pid}/{fd}"
            if client_id is not None and dedupe_key in seen_clients:
                continue
            seen_clients.add(dedupe_key)
            for engine, ns in engines.items():
                totals[engine] = totals.get(engine, 0) + ns
    return totals


def _app_gpu_busy_percent() -> float | None:
    """% d'occupation GPU de l'app : delta des compteurs fdinfo / temps.

    On prend le moteur le PLUS occupé (video pour l'encodage QSV) : c'est
    la lecture « à quel point mon GPU encode » attendue sur le dashboard."""
    global _prev_gpu
    current = _sample_drm_engines()
    now = time.monotonic()
    prev = _prev_gpu
    _prev_gpu = (now, current)
    if prev is None:
        return None
    if not current:
        # aucun client DRM ouvert (pas d'encodage en cours) : 0 % réel
        return 0.0
    elapsed_ns = (now - prev[0]) * 1e9
    if elapsed_ns <= 0:
        return None
    best = 0.0
    for engine, ns in current.items():
        prev_ns = prev[1].get(engine)
        if prev_ns is None or ns < prev_ns:
            continue
        best = max(best, (ns - prev_ns) / elapsed_ns * 100.0)
    return round(min(100.0, best), 1)


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


def _gpu_info() -> dict[str, Any]:
    """Utilisation GPU de l'app (fdinfo DRM) + fréquences sysfs."""
    out: dict[str, Any] = {"busy_percent": None, "freq_mhz": None, "freq_max_mhz": None}
    for candidate in ("/sys/class/drm/card0", "/sys/class/drm/card1"):
        cur = _read(f"{candidate}/gt_cur_freq_mhz")
        maxi = _read(f"{candidate}/gt_max_freq_mhz")
        if cur and maxi:
            with contextlib.suppress(ValueError):
                out["freq_mhz"] = int(cur.strip())
                out["freq_max_mhz"] = int(maxi.strip())
            break
    out["busy_percent"] = _app_gpu_busy_percent()
    return out


async def sample_resources(drm_node: str = "/dev/dri/renderD128") -> dict[str, Any]:
    """Instantané CPU/RAM/GPU. Champs null si indisponibles."""
    _ = drm_node
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

    gpu = _gpu_info()

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
