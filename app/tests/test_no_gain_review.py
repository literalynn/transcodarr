"""Réévaluation des « sans gain » : seules les décisions ESTIMÉES reviennent.

Constaté en production (0.9) : l'abandon anticipé jugeait « sans gain » des
épisodes qui gagnent 44 à 58 % encodés en entier. Un gain mesuré après un
encodage complet reste acquis ; une décision estimée repasse la prédiction
actuelle (confirmée sans encoder si le bitrate est déjà sous la cible)."""

from __future__ import annotations

import uuid

import pytest

from tests.test_pipeline_db import _db_reachable
from transcoder.core.queue import (
    NO_GAIN_EARLY_ABORT,
    NO_GAIN_MEASURED,
    NO_GAIN_PREDICTED,
    no_gain_origin,
)


def test_no_gain_origin() -> None:
    assert no_gain_origin("standard", "abandon anticipé : taille projetée au-dessus du seuil de gain") == NO_GAIN_EARLY_ABORT
    assert no_gain_origin(None, "abandon anticipé : taille projetée au-dessus du seuil de gain") == NO_GAIN_EARLY_ABORT
    assert no_gain_origin("preflight", "gain prévu insuffisant : bitrate…") == NO_GAIN_PREDICTED
    # Contrôle 7 après encodage complet : pas de message, gain mesuré
    assert no_gain_origin("standard", None) == NO_GAIN_MEASURED
    assert no_gain_origin(None, None) == NO_GAIN_MEASURED
    # Abandon confirmé sur deux mesures (0.10.1+) : décision fiable, conservée
    confirmed = {"early_abort": {"percent": 26.0, "alert_percent": 15.0}}
    assert no_gain_origin("standard", "abandon anticipé : taille projetée…", confirmed) == NO_GAIN_MEASURED


@pytest.mark.skipif(not _db_reachable(), reason="PostgreSQL indisponible")
async def test_review_requeues_only_estimated_decisions_with_hope() -> None:
    import datetime

    from sqlalchemy import select

    from transcoder.core.dovi import DvCapabilities
    from transcoder.core.queue import review_estimated_no_gain
    from transcoder.db import dispose_engine, session_scope
    from transcoder.enums import Codec, JobState, MediaStatus, ResolutionBucket
    from transcoder.models import BitrateRule, Job, Library, MediaFile

    marker = uuid.uuid4().hex[:8]
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    caps = DvCapabilities(libplacebo_dv=True, libplacebo=True)
    abort_msg = "abandon anticipé : taille projetée au-dessus du seuil de gain"
    async with session_scope() as session:
        lib = Library(name=f"nogain-{marker}", path=f"/nogain-{marker}", scan_cron=None)
        session.add(lib)
        await session.flush()
        session.add(
            BitrateRule(
                library_id=lib.id, codec=Codec.HEVC, resolution_bucket=ResolutionBucket.P1080,
                is_hdr=False, target_bitrate_kbps=3000, max_bitrate_kbps=4500, bufsize_kbps=6000,
            )
        )

        def media(name: str, kbps: int) -> MediaFile:
            # 24 min, fichier dont la vidéo représente l'essentiel
            return MediaFile(
                library_id=lib.id, path=f"/nogain-{marker}/{name}.mkv",
                status=MediaStatus.SKIPPED_NO_GAIN, size_bytes=int(kbps * 1440 * 125 * 1.05),
                duration_s=1440, video_codec="h264", width=1920, height=1080,
                resolution_bucket=ResolutionBucket.P1080, video_bitrate_kbps=kbps,
                bit_depth=8, is_hdr=False, mtime=now,
            )

        hopeful_abort = media("abandon-prometteur", 8000)
        hopeless_abort = media("abandon-sous-la-cible", 2000)
        measured = media("mesure", 8000)
        predicted = media("prediction-scan", 8000)
        predicted.last_error = "gain prévu insuffisant : ancienne prédiction"
        session.add_all([hopeful_abort, hopeless_abort, measured, predicted])
        await session.flush()
        for mf, message in ((hopeful_abort, abort_msg), (hopeless_abort, abort_msg), (measured, None)):
            session.add(
                Job(
                    media_file_id=mf.id, library_id=lib.id, state=JobState.SKIPPED_NO_GAIN,
                    dry_run=False, rule_hash="old", error_message=message, strategy="standard",
                    finished_at=now,
                )
            )
        lib_id = lib.id
        ids = {n: m.id for n, m in (("hopeful", hopeful_abort), ("hopeless", hopeless_abort),
                                     ("measured", measured), ("predicted", predicted))}

    async def statuses() -> dict[str, MediaStatus]:
        async with session_scope() as session:
            rows = (await session.execute(select(MediaFile.id, MediaFile.status).where(
                MediaFile.library_id == lib_id))).all()
        by_id = dict(rows)
        return {name: by_id[file_id] for name, file_id in ids.items()}

    try:
        # Aperçu : bilan exact, rien de modifié
        async with session_scope() as session:
            preview = await review_estimated_no_gain(
                session, dv_caps=caps, library_id=lib_id, apply=False
            )
        assert preview.as_dict() == {
            "measured_kept": 1, "early_aborts": 2, "predictions": 1,
            "still_hopeless": 1, "requeued": 2, "not_evaluable": 0,
        }
        assert set((await statuses()).values()) == {MediaStatus.SKIPPED_NO_GAIN}

        async with session_scope() as session:
            done = await review_estimated_no_gain(session, dv_caps=caps, library_id=lib_id)
        assert done.as_dict() == preview.as_dict()
        after = await statuses()
        assert after["hopeful"] is MediaStatus.QUEUED
        assert after["predicted"] is MediaStatus.QUEUED
        assert after["hopeless"] is MediaStatus.SKIPPED_NO_GAIN
        assert after["measured"] is MediaStatus.SKIPPED_NO_GAIN
        async with session_scope() as session:
            hopeless = await session.get(MediaFile, ids["hopeless"])
            assert hopeless is not None and hopeless.last_error is not None
            assert hopeless.last_error.startswith("gain prévu insuffisant")
            assert hopeless.applied_rule_hash not in (None, "old")

        # Deuxième passage : plus rien d'estimé à remettre en file
        async with session_scope() as session:
            again = await review_estimated_no_gain(session, dv_caps=caps, library_id=lib_id)
        assert again.requeued == 0 and again.still_hopeless == 1
    finally:
        async with session_scope() as session:
            lib_obj = await session.get(Library, lib_id)
            if lib_obj is not None:
                await session.delete(lib_obj)
        await dispose_engine()
