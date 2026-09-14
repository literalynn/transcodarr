"""Stratégies d'encodage et politique de repli (fallback).

Un échec n'est plus une impasse : chaque tentative est faite avec une
stratégie, et la catégorie de l'échec détermine la stratégie suivante.
Les stratégies sont CUMULATIVES (chacune inclut la robustesse des
précédentes) et ordonnées par coût/risque croissant :

- ``standard``      : commande nominale + plan de pistes compatible
                      (conversions/exclusions automatiques et déterministes) ;
- ``tolerant``      : démultiplexage/décodage tolérant (horodatages
                      régénérés, trames corrompues écartées, erreurs de
                      décodage ignorées) — sources abîmées, AVI/TS anciens ;
- ``reencode_subs`` : + sous-titres SubRip/texte RECONSTRUITS au lieu d'être
                      copiés (réencodage sans perte : paquets et horodatages
                      refaits) ; ASS/SSA jamais touchés (typesetting) ;
- ``mkvmerge_mux``  : + fichier final ASSEMBLÉ par mkvmerge : FFmpeg ne produit
                      que la vidéo (et les conversions), audio/sous-titres/
                      pièces jointes/chapitres recopiés bit à bit depuis la
                      source (sources Matroska aux blocs désordonnés) ;
- ``safe_streams``  : + liste blanche stricte des pistes (audio exotique →
                      FLAC, seuls les sous-titres impossibles à écrire sont
                      exclus — jamais un ASS —, données exclues) ;
- ``alt_api``       : + autre API matérielle sur le même GPU (QSV ↔ VAAPI),
                      pour les erreurs propres à l'encodeur.

Toutes les stratégies restent soumises aux mêmes validations bloquantes :
un repli ne dégrade jamais la garantie « original jamais abîmé ».
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum

from transcoder.core.failures import FailureCategory


class Strategy(str, Enum):
    STANDARD = "standard"
    TOLERANT = "tolerant"
    REENCODE_SUBS = "reencode_subs"
    MKVMERGE_MUX = "mkvmerge_mux"
    SAFE_STREAMS = "safe_streams"
    ALT_API = "alt_api"
    # Pseudo-stratégie : fichier évalué avant encodage (gain prévu nul…)
    PREFLIGHT = "preflight"

    @property
    def level(self) -> int:
        return _LEVELS[self]

    @property
    def tolerant_input(self) -> bool:
        return self.level >= _LEVELS[Strategy.TOLERANT]

    @property
    def reencode_subtitles(self) -> bool:
        return self.level >= _LEVELS[Strategy.REENCODE_SUBS]

    @property
    def mkvmerge_assembly(self) -> bool:
        return self.level >= _LEVELS[Strategy.MKVMERGE_MUX]

    @property
    def strict_streams(self) -> bool:
        return self.level >= _LEVELS[Strategy.SAFE_STREAMS]

    @property
    def alternate_api(self) -> bool:
        return self is Strategy.ALT_API


_LEVELS: dict[Strategy, int] = {
    Strategy.PREFLIGHT: -1,
    Strategy.STANDARD: 0,
    Strategy.TOLERANT: 1,
    Strategy.REENCODE_SUBS: 2,
    Strategy.MKVMERGE_MUX: 3,
    Strategy.SAFE_STREAMS: 4,
    Strategy.ALT_API: 5,
}

ENCODING_STRATEGIES: tuple[Strategy, ...] = (
    Strategy.STANDARD,
    Strategy.TOLERANT,
    Strategy.REENCODE_SUBS,
    Strategy.MKVMERGE_MUX,
    Strategy.SAFE_STREAMS,
    Strategy.ALT_API,
)

# Candidats de repli par catégorie, dans l'ordre d'essai. Une catégorie
# absente de la table n'a pas de repli (échec définitif ou transitoire).
_FALLBACKS: dict[FailureCategory, tuple[Strategy, ...]] = {
    # Sous-titres : on RÉENCODE avant d'envisager d'exclure quoi que ce soit
    FailureCategory.MUX_SUBTITLE: (
        Strategy.REENCODE_SUBS, Strategy.MKVMERGE_MUX, Strategy.SAFE_STREAMS,
    ),
    FailureCategory.MUX_AUDIO: (Strategy.SAFE_STREAMS,),
    FailureCategory.MUX_DATA: (Strategy.SAFE_STREAMS,),
    FailureCategory.MUX_OTHER: (
        Strategy.REENCODE_SUBS, Strategy.MKVMERGE_MUX, Strategy.SAFE_STREAMS, Strategy.ALT_API,
    ),
    FailureCategory.SOURCE_CORRUPT: (Strategy.TOLERANT, Strategy.SAFE_STREAMS),
    FailureCategory.TIMESTAMPS: (
        Strategy.TOLERANT, Strategy.REENCODE_SUBS, Strategy.MKVMERGE_MUX, Strategy.SAFE_STREAMS,
    ),
    FailureCategory.DURATION_MISMATCH: (Strategy.TOLERANT, Strategy.SAFE_STREAMS),
    FailureCategory.STREAM_MISMATCH: (
        Strategy.REENCODE_SUBS, Strategy.MKVMERGE_MUX, Strategy.SAFE_STREAMS,
    ),
    # Paquets de sous-titres déplacés par le muxer FFmpeg : assemblage mkvmerge,
    # qui recopie les blocs tels quels (un réencodage complet de plus pour
    # reconstruire des SubRip n'apporterait rien de plus)
    FailureCategory.SUBTITLE_TIMING: (Strategy.MKVMERGE_MUX,),
    FailureCategory.OUTPUT_CORRUPT: (Strategy.TOLERANT, Strategy.ALT_API),
    FailureCategory.VALIDATOR_CRASH: (Strategy.TOLERANT, Strategy.ALT_API),
    FailureCategory.CODEC_MISMATCH: (Strategy.ALT_API,),
    FailureCategory.OUTPUT_MISSING: (Strategy.TOLERANT, Strategy.SAFE_STREAMS),
    FailureCategory.HW_ENCODER: (Strategy.ALT_API, Strategy.TOLERANT),
    FailureCategory.ENCODER_CRASH: (Strategy.TOLERANT, Strategy.ALT_API),
    FailureCategory.UNKNOWN: (Strategy.TOLERANT, Strategy.SAFE_STREAMS, Strategy.ALT_API),
}

# Échecs dus à l'environnement (pas au fichier) : même stratégie, retentée
# après un délai, dans la limite de max_retries.
_TRANSIENT: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.INTERRUPTED,
        FailureCategory.DISK_SPACE,
        FailureCategory.SOURCE_BUSY,
        FailureCategory.HW_DEVICE,
        FailureCategory.INTERNAL,
    }
)


def is_transient(category: FailureCategory) -> bool:
    return category in _TRANSIENT


def has_fallback(category: FailureCategory) -> bool:
    return category in _FALLBACKS


def next_strategy(
    category: FailureCategory,
    tried: Iterable[Strategy | str],
    *,
    alt_api_available: bool,
    changes_command: Callable[[Strategy], bool] | None = None,
) -> Strategy | None:
    """Prochaine stratégie à essayer après un échec, ou None (définitif).

    Ne propose jamais une stratégie déjà tentée, ni ``alt_api`` quand le
    GPU n'a pas d'API alternative fonctionnelle. Les stratégies étant
    cumulatives, une stratégie de niveau inférieur à la plus haute déjà
    tentée n'apporterait rien : elle est sautée. ``changes_command`` écarte
    aussi une stratégie qui produirait la MÊME commande pour ce fichier
    (ex. repli sous-titres sur un fichier dont les sous-titres sont en ASS,
    jamais réencodés) : un encodage complet refait pour rien.
    """
    tried_set = {Strategy(t) if isinstance(t, str) else t for t in tried}
    highest = max((s.level for s in tried_set if s in ENCODING_STRATEGIES), default=-1)
    for candidate in _FALLBACKS.get(category, ()):
        if candidate in tried_set or candidate.level <= highest:
            continue
        if candidate is Strategy.ALT_API and not alt_api_available:
            continue
        if changes_command is not None and not changes_command(candidate):
            continue
        return candidate
    return None


def parse_strategy(value: str | None) -> Strategy:
    """Stratégie stockée en base (texte) → enum ; inconnue/vide = standard."""
    if not value:
        return Strategy.STANDARD
    try:
        strategy = Strategy(value)
    except ValueError:
        return Strategy.STANDARD
    return Strategy.STANDARD if strategy is Strategy.PREFLIGHT else strategy
