"""Données de seed — structures pures, sans dépendance DB (testables).

Les matrices reprennent EXACTEMENT les tables du cahier des charges (§4) :
- HEVC : valeurs réelles Tdarr (SDR + 4K HDR exactes, autres HDR = SDR × 1,25) ;
- AV1  : dérivées HEVC × 0,75, valeurs figées ici (aucun facteur au runtime).

max_bitrate = target × 1,5 ; bufsize = target × 2 (défauts §4, arrondis à l'entier).
"""

from __future__ import annotations

from dataclasses import dataclass

from transcoder.enums import Codec, ResolutionBucket

# Ordre des buckets, du plus haut au plus bas
BUCKETS: tuple[ResolutionBucket, ...] = (
    ResolutionBucket.P2160,
    ResolutionBucket.P1440,
    ResolutionBucket.P1080,
    ResolutionBucket.P720,
    ResolutionBucket.P576,
    ResolutionBucket.P480,
)

LIBRARY_NAMES: tuple[str, ...] = ("Films", "Séries", "Animés")

# Bibliothèques à seeder : (nom, chemin)
SEED_LIBRARIES: tuple[tuple[str, str], ...] = (
    ("Films", "/srv/storage/Films"),
    ("Séries", "/srv/storage/Séries"),
    ("Animés", "/srv/storage/Animés"),
)

# codec -> bibliothèque -> bucket -> (target_sdr_kbps, target_hdr_kbps)
SEED_BITRATES: dict[Codec, dict[str, dict[ResolutionBucket, tuple[int, int]]]] = {
    Codec.HEVC: {
        "Films": {
            ResolutionBucket.P2160: (12000, 15000),
            ResolutionBucket.P1440: (8000, 10000),
            ResolutionBucket.P1080: (6000, 7500),
            ResolutionBucket.P720: (3000, 3750),
            ResolutionBucket.P576: (1400, 1750),
            ResolutionBucket.P480: (1000, 1250),
        },
        "Séries": {
            ResolutionBucket.P2160: (8000, 12000),
            ResolutionBucket.P1440: (6000, 7500),
            ResolutionBucket.P1080: (4000, 5000),
            ResolutionBucket.P720: (2000, 2500),
            ResolutionBucket.P576: (1100, 1375),
            ResolutionBucket.P480: (800, 1000),
        },
        "Animés": {
            ResolutionBucket.P2160: (8000, 10000),
            ResolutionBucket.P1440: (5000, 6250),
            ResolutionBucket.P1080: (4000, 5000),
            ResolutionBucket.P720: (2000, 2500),
            ResolutionBucket.P576: (900, 1125),
            ResolutionBucket.P480: (600, 750),
        },
    },
    Codec.AV1: {
        "Films": {
            ResolutionBucket.P2160: (9000, 11000),
            ResolutionBucket.P1440: (6000, 7500),
            ResolutionBucket.P1080: (4500, 5600),
            ResolutionBucket.P720: (2250, 2800),
            ResolutionBucket.P576: (1050, 1300),
            ResolutionBucket.P480: (750, 950),
        },
        "Séries": {
            ResolutionBucket.P2160: (6000, 9000),
            ResolutionBucket.P1440: (4500, 5600),
            ResolutionBucket.P1080: (3000, 3750),
            ResolutionBucket.P720: (1500, 1900),
            ResolutionBucket.P576: (850, 1050),
            ResolutionBucket.P480: (600, 750),
        },
        "Animés": {
            ResolutionBucket.P2160: (6000, 7500),
            ResolutionBucket.P1440: (3750, 4700),
            ResolutionBucket.P1080: (3000, 3750),
            ResolutionBucket.P720: (1500, 1900),
            ResolutionBucket.P576: (700, 850),
            ResolutionBucket.P480: (450, 550),
        },
    },
}


@dataclass(frozen=True, slots=True)
class SeedRule:
    library_name: str
    codec: Codec
    bucket: ResolutionBucket
    is_hdr: bool
    target_kbps: int
    max_kbps: int
    bufsize_kbps: int


def default_max_kbps(target_kbps: int) -> int:
    """max_bitrate par défaut = target × 1,5 (§4)."""
    return round(target_kbps * 1.5)


def default_bufsize_kbps(target_kbps: int) -> int:
    """bufsize par défaut = target × 2 (§4)."""
    return target_kbps * 2


def iter_seed_rules() -> list[SeedRule]:
    """Aplati les matrices en 72 règles (3 bib × 2 codecs × 6 buckets × SDR/HDR)."""
    rules: list[SeedRule] = []
    for codec, per_lib in SEED_BITRATES.items():
        for lib_name in LIBRARY_NAMES:
            per_bucket = per_lib[lib_name]
            for bucket in BUCKETS:
                sdr_kbps, hdr_kbps = per_bucket[bucket]
                for is_hdr, target in ((False, sdr_kbps), (True, hdr_kbps)):
                    rules.append(
                        SeedRule(
                            library_name=lib_name,
                            codec=codec,
                            bucket=bucket,
                            is_hdr=is_hdr,
                            target_kbps=target,
                            max_kbps=default_max_kbps(target),
                            bufsize_kbps=default_bufsize_kbps(target),
                        )
                    )
    return rules


# --- GPU initial ------------------------------------------------------------
SEED_GPU: tuple[str, str, str, int] = ("Intel Arc A770", "/dev/dri/renderD128", "qsv", 1)


def default_app_settings(
    *,
    dry_run: bool,
    trash_retention_days: int,
    max_concurrent_jobs: int,
    data_dir: str,
) -> dict[str, object]:
    """Valeurs initiales d'app_settings (insert-if-missing, jamais écrasées)."""
    return {
        "ffmpeg_path": "/usr/lib/jellyfin-ffmpeg/ffmpeg",
        "ffprobe_path": "/usr/lib/jellyfin-ffmpeg/ffprobe",
        "dovi_tool_path": "/usr/local/bin/dovi_tool",
        "mkvpropedit_path": "/usr/bin/mkvpropedit",
        # sibling = <dossier_du_fichier>/.transcoder_tmp (rename atomique garanti) ;
        # data_tmp n'est utilisé que si même filesystem (détection st_dev, §9.3)
        "tmp_strategy": "sibling",
        "tmp_dir": f"{data_dir}/tmp",
        "trash_dir": f"{data_dir}/trash",
        "dry_run_out_dir": f"{data_dir}/out",
        "log_dir": f"{data_dir}/logs",
        "trash_retention_days": trash_retention_days,
        "log_retention_days": 90,
        "immediate_delete": False,  # « suppression immédiate » désactivée (§5.6)
        "dry_run_global": dry_run,
        "max_concurrent_jobs": max_concurrent_jobs,
        "min_free_disk_gb": 20,
        "queue_paused": False,
        # Validation DV anti-vert/violet (§5.5.9)
        "dv_ssim_threshold": 0.90,
        "dv_chroma_delta_max": 8.0,
        # full = décodage CPU intégral (§5.5.5) ; sample = 3 segments de 30 s
        "validation_decode": "full",
        "max_retries": 2,
        # Abandon anticipé si la taille projetée dépasse le seuil de gain (§5.5.7)
        "early_abort_no_gain": True,
        "progress_snapshot_interval_s": 10,
        # Intégration Jellyfin optionnelle (DateCreated, §5.1)
        "jellyfin_url": None,
        "jellyfin_api_key": None,
    }
