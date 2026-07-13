"""Résolution de la règle de bitrate applicable + hash anti-boucle (§5.7).

Le hash inclut TOUT ce qui change le résultat d'un encodage : codec, bucket,
SDR/HDR, les trois bitrates, le conteneur et les politiques HDR/DV. Basculer
une bibliothèque HEVC↔AV1 change donc le hash de tous ses fichiers → re-queue
légitime (§4).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Protocol

from transcoder import TOOL_ID
from transcoder.enums import Codec, Container, DvPolicy, HdrPolicy, ResolutionBucket


class RuleLike(Protocol):
    """Attributs minimaux d'une règle (satisfait par models.BitrateRule)."""

    codec: Codec
    resolution_bucket: ResolutionBucket
    is_hdr: bool
    target_bitrate_kbps: int
    max_bitrate_kbps: int
    bufsize_kbps: int


@dataclass(frozen=True, slots=True)
class ResolvedRule:
    """Règle applicable à un fichier, avec son hash anti-boucle."""

    codec: Codec
    bucket: ResolutionBucket
    is_hdr: bool
    target_kbps: int
    max_kbps: int
    bufsize_kbps: int
    rule_hash: str


class NoRuleError(LookupError):
    """Aucune règle (codec, bucket, is_hdr) pour cette bibliothèque."""


def compute_rule_hash(
    *,
    codec: Codec,
    bucket: ResolutionBucket,
    is_hdr: bool,
    target_kbps: int,
    max_kbps: int,
    bufsize_kbps: int,
    container: Container,
    hdr_policy: HdrPolicy,
    dv_policy: DvPolicy,
) -> str:
    """Hash stable (16 hex) de la configuration effective d'encodage."""
    canonical = "|".join(
        (
            codec.value,
            bucket.value,
            "hdr" if is_hdr else "sdr",
            str(target_kbps),
            str(max_kbps),
            str(bufsize_kbps),
            container.value,
            hdr_policy.value,
            dv_policy.value,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def resolve_rule(
    rules: Iterable[RuleLike],
    *,
    codec: Codec,
    bucket: ResolutionBucket,
    is_hdr: bool,
    container: Container,
    hdr_policy: HdrPolicy,
    dv_policy: DvPolicy,
) -> ResolvedRule:
    """Sélectionne la règle (codec, bucket, is_hdr) et calcule son hash.

    `is_hdr` est le is_hdr EFFECTIF issu de la matrice DV (un DV 8.2 est
    traité avec la règle SDR, §5.3.1) — pas nécessairement celui de la sonde.
    """
    for r in rules:
        if r.codec == codec and r.resolution_bucket == bucket and r.is_hdr == is_hdr:
            return ResolvedRule(
                codec=codec,
                bucket=bucket,
                is_hdr=is_hdr,
                target_kbps=r.target_bitrate_kbps,
                max_kbps=r.max_bitrate_kbps,
                bufsize_kbps=r.bufsize_kbps,
                rule_hash=compute_rule_hash(
                    codec=codec,
                    bucket=bucket,
                    is_hdr=is_hdr,
                    target_kbps=r.target_bitrate_kbps,
                    max_kbps=r.max_bitrate_kbps,
                    bufsize_kbps=r.bufsize_kbps,
                    container=container,
                    hdr_policy=hdr_policy,
                    dv_policy=dv_policy,
                ),
            )
    raise NoRuleError(
        f"aucune règle de bitrate pour ({codec.value}, {bucket.value}, "
        f"{'HDR' if is_hdr else 'SDR'})"
    )


def build_transcoder_tag(rule: ResolvedRule) -> str:
    """Tag conteneur anti-boucle (§5.7) : jftranscoder-v1|codec=…|br=…|hash."""
    return f"{TOOL_ID}|codec={rule.codec.value}|br={rule.target_kbps}|{rule.rule_hash}"


def parse_transcoder_tag(tag: str) -> tuple[str, str] | None:
    """Extrait (tool_id, rule_hash) d'un tag conteneur ; None si illisible."""
    parts = tag.split("|")
    if len(parts) != 4:
        return None
    return parts[0], parts[3]
