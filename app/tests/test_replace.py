"""Remplacement atomique §5.6 — tests sur filesystem temporaire réel."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from transcoder.core.dovi import default_hdr10_metadata
from transcoder.core.replace import (
    DiskSpaceError,
    ReplaceError,
    check_free_space,
    make_dry_run_path,
    make_tmp_path,
    mkvpropedit_args,
    purge_orphan_tmp,
    purge_trash,
    replace_original,
    restore_from_trash,
)
from transcoder.enums import Container


def _make_src(tmp_path: Path, name: str = "Films/film.mkv", content: bytes = b"O" * 1000) -> Path:
    src = tmp_path / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(content)
    os.utime(src, (1_600_000_000, 1_500_000_000))
    return src


def _make_tmp_out(src: Path, content: bytes = b"N" * 400) -> Path:
    tmp_dir = src.parent / ".transcoder_tmp"
    tmp_dir.mkdir(exist_ok=True)
    out = tmp_dir / "abc.mkv"
    out.write_bytes(content)
    return out


# ---------------------------------------------------------------------------
# Espace disque et chemins temporaires
# ---------------------------------------------------------------------------
def test_check_free_space_raises_when_insufficient(tmp_path: Path) -> None:
    with pytest.raises(DiskSpaceError):
        check_free_space(str(tmp_path), needed_bytes=0, min_free_gb=10_000_000)
    # Cas nominal : ne lève pas
    check_free_space(str(tmp_path), needed_bytes=1024, min_free_gb=0)


def test_make_tmp_path_sibling(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    p = make_tmp_path(str(src), container=Container.MKV, tmp_strategy="sibling", tmp_dir="/nope")
    assert p.startswith(str(src.parent / ".transcoder_tmp"))
    assert p.endswith(".mkv")
    assert os.path.isdir(os.path.dirname(p))


def test_make_tmp_path_data_tmp_same_fs(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    data_tmp = tmp_path / "data_tmp"
    data_tmp.mkdir()
    p = make_tmp_path(
        str(src), container=Container.MKV, tmp_strategy="data_tmp", tmp_dir=str(data_tmp)
    )
    # Même filesystem (même tmp_path) → data_tmp accepté
    assert p.startswith(str(data_tmp))


def test_make_tmp_path_data_tmp_unreachable_falls_back(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    p = make_tmp_path(
        str(src),
        container=Container.MKV,
        tmp_strategy="data_tmp",
        tmp_dir=str(tmp_path / "does-not-exist"),
    )
    assert p.startswith(str(src.parent / ".transcoder_tmp"))


def test_make_dry_run_path_mirrors_tree(tmp_path: Path) -> None:
    out = make_dry_run_path(
        "/srv/storage/Films/A/film.mkv",
        library_path="/srv/storage/Films",
        out_dir=str(tmp_path / "out"),
        container=Container.MKV,
    )
    assert out == str(tmp_path / "out" / "A" / "film.mkv")
    assert os.path.isdir(os.path.dirname(out))


# ---------------------------------------------------------------------------
# Remplacement
# ---------------------------------------------------------------------------
def test_replace_nominal_trash_and_metadata(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    out = _make_tmp_out(src)
    trash = tmp_path / "trash"

    result = replace_original(
        src_path=str(src),
        tmp_path=str(out),
        job_id=42,
        trash_dir=str(trash),
        immediate_delete=False,
        container=Container.MKV,
    )
    assert result.final_path == str(src)
    assert not result.extension_changed
    # Le nouveau contenu est en place
    assert src.read_bytes() == b"N" * 400
    # L'original est en corbeille, intact
    assert result.trashed_path is not None
    assert "job42" in result.trashed_path
    assert Path(result.trashed_path).read_bytes() == b"O" * 1000
    # mtime restauré (§5.6)
    assert os.stat(src).st_mtime == pytest.approx(1_500_000_000)
    # le temporaire a disparu
    assert not out.exists()


def test_replace_extension_change(tmp_path: Path) -> None:
    src = _make_src(tmp_path, "Films/film.avi")
    out = _make_tmp_out(src)
    result = replace_original(
        src_path=str(src),
        tmp_path=str(out),
        job_id=1,
        trash_dir=str(tmp_path / "trash"),
        immediate_delete=False,
        container=Container.MKV,
    )
    assert result.extension_changed
    assert result.final_path.endswith("film.mkv")
    assert Path(result.final_path).exists()
    assert not src.exists()  # l'ancien chemin est parti en corbeille


def test_replace_immediate_delete(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    out = _make_tmp_out(src)
    result = replace_original(
        src_path=str(src),
        tmp_path=str(out),
        job_id=1,
        trash_dir=str(tmp_path / "trash"),
        immediate_delete=True,
        container=Container.MKV,
    )
    assert result.trashed_path is None
    assert src.read_bytes() == b"N" * 400
    assert not (tmp_path / "trash").exists()


def test_replace_missing_tmp_keeps_original(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    with pytest.raises(ReplaceError):
        replace_original(
            src_path=str(src),
            tmp_path=str(tmp_path / "nonexistent.mkv"),
            job_id=1,
            trash_dir=str(tmp_path / "trash"),
            immediate_delete=False,
            container=Container.MKV,
        )
    assert src.read_bytes() == b"O" * 1000  # original intact


def test_restore_from_trash(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    out = _make_tmp_out(src)
    result = replace_original(
        src_path=str(src),
        tmp_path=str(out),
        job_id=7,
        trash_dir=str(tmp_path / "trash"),
        immediate_delete=False,
        container=Container.MKV,
    )
    assert result.trashed_path is not None
    # Rollback refuse d'écraser le fichier transcodé en place
    with pytest.raises(ReplaceError):
        restore_from_trash(result.trashed_path, str(src))
    src.unlink()
    restore_from_trash(result.trashed_path, str(src))
    assert src.read_bytes() == b"O" * 1000


# ---------------------------------------------------------------------------
# Purges
# ---------------------------------------------------------------------------
def test_purge_trash_respects_retention(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    old = trash / "20200101-000000-job1"
    new = trash / "recent-job2"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "a.mkv").write_bytes(b"x")
    (new / "b.mkv").write_bytes(b"y")
    os.utime(old, (1_577_836_800, 1_577_836_800))  # 2020
    removed = purge_trash(str(trash), retention_days=14)
    assert removed == 1
    assert not old.exists()
    assert new.exists()


def test_purge_orphan_tmp(tmp_path: Path) -> None:
    lib = tmp_path / "Films"
    hidden = lib / "Sub" / ".transcoder_tmp"
    hidden.mkdir(parents=True)
    (hidden / "orphan.mkv").write_bytes(b"z")
    extra = tmp_path / "data_tmp"
    extra.mkdir()
    (extra / "orphan2.mkv").write_bytes(b"z")
    removed = purge_orphan_tmp([str(lib)], extra_tmp_dir=str(extra))
    assert removed == 2
    assert not (hidden / "orphan.mkv").exists()


# ---------------------------------------------------------------------------
# mkvpropedit (arbitrage 1) — arguments purs
# ---------------------------------------------------------------------------
def test_mkvpropedit_args_defaults() -> None:
    args = mkvpropedit_args("/x/out.mkv", default_hdr10_metadata())
    joined = " ".join(args)
    assert args[0] == "/x/out.mkv"
    assert "--edit track:v1" in joined
    assert "chromaticity-coordinates-red-x=0.68000" in joined
    assert "white-coordinates-x=0.31270" in joined
    assert "min-luminance=0.0001" in joined
    assert "max-luminance=1000.0000" in joined
    assert "max-content-light=1000" in joined
    assert "max-frame-light=400" in joined


def test_purge_old_logs(tmp_path: Path) -> None:
    from transcoder.core.replace import purge_old_logs

    logs = tmp_path / "logs"
    logs.mkdir()
    old = logs / "job_1.log"
    new = logs / "job_2.log"
    old.write_text("x")
    new.write_text("y")
    os.utime(old, (1_577_836_800, 1_577_836_800))  # 2020
    assert purge_old_logs(str(logs), retention_days=90) == 1
    assert not old.exists() and new.exists()
    assert purge_old_logs(str(tmp_path / "absent"), retention_days=90) == 0
