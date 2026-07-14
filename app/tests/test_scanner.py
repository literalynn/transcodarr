"""Scanner : décision anti-boucle §5.7, filtres §5.1, gain estimé §5.2."""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from transcoder.core.scanner import (
    Decision,
    FileFacts,
    added_date,
    decide_status,
    estimate_gain_bytes,
    file_matches_filters,
)
from transcoder.core.dovi import TreatmentAction
from transcoder.enums import MediaStatus

TAG_OK = "jftranscoder-v1|codec=hevc|br=6000|aaaa000011112222"
HASH_OK = "aaaa000011112222"
HASH_NEW = "bbbb000011112222"


def _facts(**overrides: object) -> FileFacts:
    base: dict[str, object] = dict(
        plan_action=TreatmentAction.SDR_PASSTHROUGH,
        rule_hash=HASH_OK,
        container_tag=None,
        date_ok=True,
        existing_status=None,
        existing_applied_hash=None,
        existing_retry_count=0,
        max_retries=2,
    )
    base.update(overrides)
    return FileFacts(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §5.7 : re-traiter UNIQUEMENT si tag absent OU rule_hash changé
# ---------------------------------------------------------------------------
def test_new_file_without_tag_is_queued() -> None:
    d = decide_status(_facts())
    assert d.status is MediaStatus.QUEUED


def test_tagged_file_same_hash_is_done() -> None:
    d = decide_status(_facts(container_tag=TAG_OK))
    assert d.status is MediaStatus.DONE


def test_tagged_file_rule_changed_is_requeued() -> None:
    """L'utilisateur a modifié sa config → re-traitement légitime, affiché."""
    d = decide_status(_facts(container_tag=TAG_OK, rule_hash=HASH_NEW))
    assert d.status is MediaStatus.QUEUED
    assert d.rule_changed


def test_foreign_tag_is_requeued() -> None:
    """Tag posé par un autre outil (ou v2) : pas le nôtre → traiter."""
    d = decide_status(_facts(container_tag="autre-outil|codec=hevc|br=1|xxxx"))
    assert d.status is MediaStatus.QUEUED
    assert not d.rule_changed


def test_unreadable_tag_is_requeued() -> None:
    d = decide_status(_facts(container_tag="garbage"))
    assert d.status is MediaStatus.QUEUED


def test_done_in_db_but_no_tag_requeues() -> None:
    """DONE en base mais fichier sans tag (remplacé hors app, ou dry-run) → re-queue."""
    d = decide_status(_facts(existing_status=MediaStatus.DONE, existing_applied_hash=HASH_OK))
    assert d.status is MediaStatus.QUEUED


# ---------------------------------------------------------------------------
# Politiques, filtres, états protégés
# ---------------------------------------------------------------------------
def test_skip_plan_gives_skipped_policy() -> None:
    d = decide_status(_facts(plan_action=TreatmentAction.SKIP))
    assert d.status is MediaStatus.SKIPPED_POLICY


def test_date_filter_gives_ignored() -> None:
    d = decide_status(_facts(date_ok=False))
    assert d.status is MediaStatus.IGNORED


def test_user_ignored_never_touched() -> None:
    d = decide_status(_facts(existing_status=MediaStatus.IGNORED))
    assert d.status is MediaStatus.IGNORED


def test_processing_untouched_but_queued_reevaluated() -> None:
    # PROCESSING : jamais touché par le scan
    d = decide_status(_facts(existing_status=MediaStatus.PROCESSING))
    assert d.status is MediaStatus.PROCESSING
    # QUEUED : re-décidé — un filtre de date ajouté doit pouvoir le retirer
    d = decide_status(_facts(existing_status=MediaStatus.QUEUED, date_ok=False))
    assert d.status is MediaStatus.IGNORED
    d = decide_status(_facts(existing_status=MediaStatus.QUEUED))
    assert d.status is MediaStatus.QUEUED


def test_no_rule_gives_failed() -> None:
    d = decide_status(_facts(rule_hash=None))
    assert d.status is MediaStatus.FAILED


def test_skipped_no_gain_not_retried_same_rule() -> None:
    """§5.5.7 : marqué traité pour ne pas être retenté indéfiniment."""
    d = decide_status(
        _facts(existing_status=MediaStatus.SKIPPED_NO_GAIN, existing_applied_hash=HASH_OK)
    )
    assert d.status is MediaStatus.SKIPPED_NO_GAIN


def test_skipped_no_gain_requeued_on_rule_change() -> None:
    d = decide_status(
        _facts(
            existing_status=MediaStatus.SKIPPED_NO_GAIN,
            existing_applied_hash=HASH_NEW,  # ancienne règle ≠ courante
        )
    )
    assert d.status is MediaStatus.QUEUED
    assert d.rule_changed


def test_failed_exhausted_not_retried() -> None:
    """§5.2 : 3 échecs → failed, plus repris automatiquement."""
    d = decide_status(
        _facts(
            existing_status=MediaStatus.FAILED,
            existing_applied_hash=HASH_OK,
            existing_retry_count=3,
        )
    )
    assert d.status is MediaStatus.FAILED


def test_failed_requeued_on_rule_change() -> None:
    d = decide_status(
        _facts(
            existing_status=MediaStatus.FAILED,
            existing_applied_hash=HASH_NEW,
            existing_retry_count=3,
        )
    )
    assert d.status is MediaStatus.QUEUED


def test_failed_with_retries_left_requeues() -> None:
    d = decide_status(
        _facts(
            existing_status=MediaStatus.FAILED,
            existing_applied_hash=HASH_OK,
            existing_retry_count=1,
        )
    )
    assert d.status is MediaStatus.QUEUED


# ---------------------------------------------------------------------------
# Filtres de fichiers
# ---------------------------------------------------------------------------
def test_extension_filter() -> None:
    exts = ["mkv", "mp4"]
    assert file_matches_filters("a/film.mkv", include_extensions=exts, exclude_globs=[])
    assert file_matches_filters("a/film.MKV", include_extensions=exts, exclude_globs=[])
    assert not file_matches_filters("a/film.avi", include_extensions=exts, exclude_globs=[])
    assert not file_matches_filters("a/film", include_extensions=exts, exclude_globs=[])


def test_exclude_globs() -> None:
    exts = ["mkv"]
    assert not file_matches_filters(
        "extras/sample.mkv", include_extensions=exts, exclude_globs=["*sample*"]
    )
    assert not file_matches_filters(
        "Behind The Scenes/x.mkv", include_extensions=exts, exclude_globs=["Behind*"]
    )
    assert file_matches_filters(
        "film.mkv", include_extensions=exts, exclude_globs=["*sample*"]
    )


# ---------------------------------------------------------------------------
# Date d'ajout et gain estimé
# ---------------------------------------------------------------------------
def test_added_date_fallback_max_mtime_ctime(tmp_path: Path) -> None:
    f = tmp_path / "x.mkv"
    f.write_bytes(b"x")
    st = os.stat(f)
    d = added_date(st, None)
    # btime (macOS) ou mtime — JAMAIS le ctime (chown/chmod le réécrivent)
    assert d.tzinfo is not None
    assert abs(d.timestamp() - st.st_mtime) < 86400


def test_added_date_jellyfin_override(tmp_path: Path) -> None:
    f = tmp_path / "x.mkv"
    f.write_bytes(b"x")
    jf = datetime.datetime(2020, 5, 1, tzinfo=datetime.timezone.utc)
    assert added_date(os.stat(f), jf) == jf


def test_estimate_gain_bytes() -> None:
    # 20 Mbps → 12 Mbps sur 3600 s = 8000 kbps × 3600 s × 125 o = 3,6 Go
    assert estimate_gain_bytes(20000, 12000, 3600.0) == 8000 * 3600 * 125
    # Cible plus haute que la source : jamais négatif
    assert estimate_gain_bytes(3000, 6000, 3600.0) == 0
    assert estimate_gain_bytes(None, 6000, 3600.0) is None
    assert estimate_gain_bytes(3000, 6000, None) is None


def test_effective_video_bitrate_fallback() -> None:
    from transcoder.core.scanner import effective_video_bitrate_kbps

    # Bitrate connu : inchangé
    assert effective_video_bitrate_kbps(8000, 10**9, 3600.0) == 8000
    # Inconnu : approximé depuis taille/durée (× 0,85 pour l'audio/conteneur)
    # 4,5 Go sur 3600 s ≈ 10 Mbps conteneur → ~8500 kbps vidéo
    approx = effective_video_bitrate_kbps(None, 4_500_000_000, 3600.0)
    assert approx is not None and 8000 < approx < 9000
    assert effective_video_bitrate_kbps(None, 0, 3600.0) is None
    assert effective_video_bitrate_kbps(None, 10**9, None) is None
