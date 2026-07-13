"""Classement d'une vidéo dans un bucket de résolution (§4).

Le classement se fait par HAUTEUR d'image, avec tolérance pour le contenu
cinéma cropé (un 3840×1600 reste du 2160p) : les seuils sont volontairement
en dessous des hauteurs nominales.
"""

from __future__ import annotations

from transcoder.enums import ResolutionBucket

# Seuils exacts du cahier des charges (§4), du plus haut au plus bas.
_THRESHOLDS: tuple[tuple[int, ResolutionBucket], ...] = (
    (1800, ResolutionBucket.P2160),
    (1200, ResolutionBucket.P1440),
    (900, ResolutionBucket.P1080),
    (620, ResolutionBucket.P720),
    (530, ResolutionBucket.P576),
)


def bucket_for_height(height: int) -> ResolutionBucket:
    """Hauteur d'image (pixels) → bucket de résolution."""
    for threshold, bucket in _THRESHOLDS:
        if height >= threshold:
            return bucket
    return ResolutionBucket.P480
