"""Résolution de règle + hash anti-boucle (§5.7) + tag conteneur."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from transcoder.core.rules import (
    NoRuleError,
    build_transcoder_tag,
    compute_rule_hash,
    parse_transcoder_tag,
    resolve_rule,
)
from transcoder.enums import Codec, Container, DvPolicy, HdrPolicy, ResolutionBucket


@dataclass
class FakeRule:
    codec: Codec
    resolution_bucket: ResolutionBucket
    is_hdr: bool
    target_bitrate_kbps: int
    max_bitrate_kbps: int
    bufsize_kbps: int


RULES = [
    FakeRule(Codec.HEVC, ResolutionBucket.P1080, False, 6000, 9000, 12000),
    FakeRule(Codec.HEVC, ResolutionBucket.P1080, True, 7500, 11250, 15000),
    FakeRule(Codec.AV1, ResolutionBucket.P1080, False, 4500, 6750, 9000),
]

_COMMON = {
    "container": Container.MKV,
    "hdr_policy": HdrPolicy.PRESERVE,
    "dv_policy": DvPolicy.CONVERT_TO_HDR10,
}


def test_resolve_picks_exact_rule() -> None:
    r = resolve_rule(
        RULES, codec=Codec.HEVC, bucket=ResolutionBucket.P1080, is_hdr=True, **_COMMON
    )
    assert r.target_kbps == 7500
    assert r.max_kbps == 11250
    assert r.bufsize_kbps == 15000
    assert len(r.rule_hash) == 16


def test_resolve_missing_rule_raises() -> None:
    with pytest.raises(NoRuleError):
        resolve_rule(
            RULES, codec=Codec.AV1, bucket=ResolutionBucket.P2160, is_hdr=False, **_COMMON
        )


def _hash(**overrides: object) -> str:
    base: dict[str, object] = dict(
        codec=Codec.HEVC,
        bucket=ResolutionBucket.P1080,
        is_hdr=False,
        target_kbps=6000,
        max_kbps=9000,
        bufsize_kbps=12000,
        container=Container.MKV,
        hdr_policy=HdrPolicy.PRESERVE,
        dv_policy=DvPolicy.CONVERT_TO_HDR10,
    )
    base.update(overrides)
    return compute_rule_hash(**base)  # type: ignore[arg-type]


def test_hash_is_stable() -> None:
    assert _hash() == _hash()


def test_hash_changes_with_codec() -> None:
    """Basculer HEVC↔AV1 doit re-queuer tous les fichiers (§4)."""
    assert _hash() != _hash(codec=Codec.AV1)


@pytest.mark.parametrize(
    "override",
    [
        {"bucket": ResolutionBucket.P720},
        {"is_hdr": True},
        {"target_kbps": 6001},
        {"max_kbps": 9001},
        {"bufsize_kbps": 12001},
        {"container": Container.MP4},
        {"hdr_policy": HdrPolicy.TONEMAP_SDR},
        {"dv_policy": DvPolicy.SKIP_DV_FILES},
    ],
)
def test_hash_sensitive_to_every_component(override: dict[str, object]) -> None:
    assert _hash() != _hash(**override)


def test_tag_roundtrip() -> None:
    r = resolve_rule(
        RULES, codec=Codec.HEVC, bucket=ResolutionBucket.P1080, is_hdr=False, **_COMMON
    )
    tag = build_transcoder_tag(r)
    assert tag == f"jftranscoder-v1|codec=hevc|br=6000|{r.rule_hash}"
    parsed = parse_transcoder_tag(tag)
    assert parsed == ("jftranscoder-v1", r.rule_hash)


def test_tag_parse_garbage() -> None:
    assert parse_transcoder_tag("n'importe quoi") is None
    assert parse_transcoder_tag("a|b") is None
