"""Verrouille les matrices de bitrate du seed (§4 du cahier des charges).

Tests purs, sans base de données : toute modification accidentelle des
valeurs seedées fait échouer la CI.
"""

from __future__ import annotations

from transcoder.enums import Codec, ResolutionBucket
from transcoder.seed_data import (
    BUCKETS,
    LIBRARY_NAMES,
    SEED_BITRATES,
    default_bufsize_kbps,
    default_max_kbps,
    iter_seed_rules,
    seed_libraries,
)


def test_rule_count() -> None:
    # 3 bibliothèques × 2 codecs × 6 buckets × (SDR + HDR) = 72
    assert len(iter_seed_rules()) == 72


def test_uniqueness() -> None:
    keys = {(r.library_name, r.codec, r.bucket, r.is_hdr) for r in iter_seed_rules()}
    assert len(keys) == 72


def test_matrices_complete() -> None:
    for codec in (Codec.HEVC, Codec.AV1):
        assert set(SEED_BITRATES[codec].keys()) == set(LIBRARY_NAMES)
        for lib in LIBRARY_NAMES:
            assert set(SEED_BITRATES[codec][lib].keys()) == set(BUCKETS)


def test_hevc_exact_values() -> None:
    """Valeurs réelles Tdarr — ne JAMAIS modifier sans décision explicite."""
    h = SEED_BITRATES[Codec.HEVC]
    # (sdr, hdr) — les 4K HDR en valeurs exactes du cahier des charges
    assert h["Movies"][ResolutionBucket.P2160] == (12000, 15000)
    assert h["TV Shows"][ResolutionBucket.P2160] == (8000, 12000)
    assert h["Anime"][ResolutionBucket.P2160] == (8000, 10000)
    assert h["Movies"][ResolutionBucket.P1440] == (8000, 10000)
    assert h["TV Shows"][ResolutionBucket.P1440] == (6000, 7500)
    assert h["Anime"][ResolutionBucket.P1440] == (5000, 6250)
    assert h["Movies"][ResolutionBucket.P1080] == (6000, 7500)
    assert h["TV Shows"][ResolutionBucket.P1080] == (4000, 5000)
    assert h["Anime"][ResolutionBucket.P1080] == (4000, 5000)
    assert h["Movies"][ResolutionBucket.P720] == (3000, 3750)
    assert h["TV Shows"][ResolutionBucket.P720] == (2000, 2500)
    assert h["Anime"][ResolutionBucket.P720] == (2000, 2500)
    assert h["Movies"][ResolutionBucket.P576] == (1400, 1750)
    assert h["TV Shows"][ResolutionBucket.P576] == (1100, 1375)
    assert h["Anime"][ResolutionBucket.P576] == (900, 1125)
    assert h["Movies"][ResolutionBucket.P480] == (1000, 1250)
    assert h["TV Shows"][ResolutionBucket.P480] == (800, 1000)
    assert h["Anime"][ResolutionBucket.P480] == (600, 750)


def test_av1_exact_values() -> None:
    """Table AV1 dérivée (HEVC × 0,75), figée au seed — aucun facteur runtime."""
    a = SEED_BITRATES[Codec.AV1]
    assert a["Movies"][ResolutionBucket.P2160] == (9000, 11000)
    assert a["TV Shows"][ResolutionBucket.P2160] == (6000, 9000)
    assert a["Anime"][ResolutionBucket.P2160] == (6000, 7500)
    assert a["Movies"][ResolutionBucket.P1440] == (6000, 7500)
    assert a["TV Shows"][ResolutionBucket.P1440] == (4500, 5600)
    assert a["Anime"][ResolutionBucket.P1440] == (3750, 4700)
    assert a["Movies"][ResolutionBucket.P1080] == (4500, 5600)
    assert a["TV Shows"][ResolutionBucket.P1080] == (3000, 3750)
    assert a["Anime"][ResolutionBucket.P1080] == (3000, 3750)
    assert a["Movies"][ResolutionBucket.P720] == (2250, 2800)
    assert a["TV Shows"][ResolutionBucket.P720] == (1500, 1900)
    assert a["Anime"][ResolutionBucket.P720] == (1500, 1900)
    assert a["Movies"][ResolutionBucket.P576] == (1050, 1300)
    assert a["TV Shows"][ResolutionBucket.P576] == (850, 1050)
    assert a["Anime"][ResolutionBucket.P576] == (700, 850)
    assert a["Movies"][ResolutionBucket.P480] == (750, 950)
    assert a["TV Shows"][ResolutionBucket.P480] == (600, 750)
    assert a["Anime"][ResolutionBucket.P480] == (450, 550)


def test_max_and_bufsize_derivation() -> None:
    for r in iter_seed_rules():
        assert r.max_kbps == default_max_kbps(r.target_kbps) == round(r.target_kbps * 1.5)
        assert r.bufsize_kbps == default_bufsize_kbps(r.target_kbps) == r.target_kbps * 2
        assert r.max_kbps >= r.target_kbps
        assert r.bufsize_kbps >= r.target_kbps


def test_seed_libraries_paths() -> None:
    expected = (
        ("Movies", "/mnt/media/Movies"),
        ("TV Shows", "/mnt/media/TV Shows"),
        ("Anime", "/mnt/media/Anime"),
    )
    assert seed_libraries("/mnt/media") == expected
    assert seed_libraries("/mnt/media/") == expected
