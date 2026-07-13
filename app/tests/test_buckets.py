"""Seuils exacts du classement en buckets (§4)."""

from __future__ import annotations

import pytest

from transcoder.core.buckets import bucket_for_height
from transcoder.enums import ResolutionBucket


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        # Bornes exactes des seuils du cahier des charges
        (2160, ResolutionBucket.P2160),
        (1800, ResolutionBucket.P2160),
        (1799, ResolutionBucket.P1440),
        (1440, ResolutionBucket.P1440),
        (1200, ResolutionBucket.P1440),
        (1199, ResolutionBucket.P1080),
        (1080, ResolutionBucket.P1080),
        (900, ResolutionBucket.P1080),
        (899, ResolutionBucket.P720),
        (720, ResolutionBucket.P720),
        (620, ResolutionBucket.P720),
        (619, ResolutionBucket.P576),
        (576, ResolutionBucket.P576),
        (530, ResolutionBucket.P576),
        (529, ResolutionBucket.P480),
        (480, ResolutionBucket.P480),
        (1, ResolutionBucket.P480),
        # Contenu cinéma cropé : un 4K scope reste du 2160p
        (1600, ResolutionBucket.P1440),
        # 1080p scope (2.39:1) : hauteur ~800 → 720p ? Non : 800 < 900.
        # C'est le comportement voulu par les seuils du §4.
        (800, ResolutionBucket.P720),
    ],
)
def test_bucket_thresholds(height: int, expected: ResolutionBucket) -> None:
    assert bucket_for_height(height) is expected
