"""Runner FFmpeg : parsing -progress (pur) + exécution avec faux binaire."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from transcoder.core.runner import ProgressUpdate, parse_progress_block, run_ffmpeg


# ---------------------------------------------------------------------------
# Parsing pur
# ---------------------------------------------------------------------------
def test_parse_progress_block_nominal() -> None:
    block = {
        "out_time_us": "60000000",
        "fps": "48.5",
        "speed": "2.02x",
        "bitrate": "5432.1kbits/s",
        "progress": "continue",
    }
    u = parse_progress_block(block, duration_s=600.0)
    assert u.out_time_s == 60.0
    assert u.percent == pytest.approx(10.0)
    assert u.fps == 48.5
    assert u.speed == 2.02
    assert u.bitrate_kbps == pytest.approx(5432.1)
    # ETA = (600 - 60) / 2.02
    assert u.eta_s == pytest.approx(267.3, abs=0.1)
    assert not u.finished


def test_parse_progress_end_and_clamp() -> None:
    u = parse_progress_block(
        {"out_time_us": "700000000", "progress": "end"}, duration_s=600.0
    )
    assert u.finished
    assert u.percent == 100.0  # clampé


def test_parse_progress_out_time_fallback() -> None:
    u = parse_progress_block({"out_time": "00:01:30.500000", "progress": "continue"}, 300.0)
    assert u.out_time_s == pytest.approx(90.5)


def test_parse_progress_na_values() -> None:
    u = parse_progress_block(
        {"out_time_us": "N/A", "fps": "N/A", "speed": "N/A", "bitrate": "N/A", "progress": "continue"},
        duration_s=None,
    )
    assert u.percent is None
    assert u.fps is None
    assert u.speed is None


# ---------------------------------------------------------------------------
# Exécution avec faux ffmpeg (script Python)
# ---------------------------------------------------------------------------
def _fake_ffmpeg(tmp_path: Path, body: str) -> str:
    # Wrapper /bin/sh : un shebang direct vers sys.executable casserait si le
    # chemin de l'interpréteur contient un espace (repo sous iCloud Drive)
    impl = tmp_path / "fake_ffmpeg_impl.py"
    impl.write_text(f"import sys, time\n{body}\n", encoding="utf-8")
    script = tmp_path / "fake_ffmpeg"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{impl}" "$@"\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_run_ffmpeg_collects_progress_and_logs(tmp_path: Path) -> None:
    fake = _fake_ffmpeg(
        tmp_path,
        """
for i in (25, 50, 75, 100):
    print(f"out_time_us={i * 6000000}")
    print("fps=50.0")
    print("speed=2.0x")
    print("progress=" + ("end" if i == 100 else "continue"))
    sys.stdout.flush()
    print(f"frame {i} warning", file=sys.stderr)
    sys.stderr.flush()
sys.exit(0)
""",
    )
    updates: list[ProgressUpdate] = []
    lines: list[str] = []

    async def on_progress(u: ProgressUpdate) -> None:
        updates.append(u)

    async def on_log(line: str) -> None:
        lines.append(line)

    log_path = str(tmp_path / "logs" / "job.log")
    result = await run_ffmpeg(
        [fake],
        duration_s=600.0,
        log_path=log_path,
        on_progress=on_progress,
        on_log_line=on_log,
    )
    assert result.exit_code == 0
    assert not result.cancelled
    assert len(updates) == 4
    assert updates[0].percent == pytest.approx(25.0)
    assert updates[-1].finished
    assert result.avg_fps == 50.0
    assert result.avg_speed == 2.0
    assert len(lines) == 4
    assert "frame 100 warning" in result.log_tail
    assert os.path.exists(log_path)
    assert "frame 25 warning" in Path(log_path).read_text(encoding="utf-8")


async def test_run_ffmpeg_nonzero_exit(tmp_path: Path) -> None:
    fake = _fake_ffmpeg(
        tmp_path,
        """
print("erreur fatale", file=sys.stderr)
sys.exit(187)
""",
    )
    result = await run_ffmpeg([fake], duration_s=None, log_path=str(tmp_path / "j.log"))
    assert result.exit_code == 187
    assert "erreur fatale" in result.log_tail


async def test_run_ffmpeg_cancellation_sends_sigterm(tmp_path: Path) -> None:
    fake = _fake_ffmpeg(
        tmp_path,
        """
import signal
def bye(sig, frame):
    print("SIGTERM reçu", file=sys.stderr)
    sys.exit(143)
signal.signal(signal.SIGTERM, bye)
print("out_time_us=1000000")
print("progress=continue")
sys.stdout.flush()
time.sleep(30)
""",
    )
    cancel = asyncio.Event()

    async def trigger() -> None:
        await asyncio.sleep(0.3)
        cancel.set()

    task = asyncio.create_task(trigger())
    result = await run_ffmpeg(
        [fake], duration_s=100.0, log_path=str(tmp_path / "j.log"), cancel_event=cancel
    )
    await task
    assert result.cancelled
    assert result.exit_code != 0


async def test_run_ffmpeg_log_tail_bounded(tmp_path: Path) -> None:
    fake = _fake_ffmpeg(
        tmp_path,
        """
for i in range(500):
    print(f"ligne {i}", file=sys.stderr)
sys.exit(0)
""",
    )
    result = await run_ffmpeg([fake], duration_s=None, log_path=str(tmp_path / "j.log"))
    tail_lines = result.log_tail.splitlines()
    assert len(tail_lines) == 200
    assert tail_lines[-1] == "ligne 499"
    assert tail_lines[0] == "ligne 300"
    # Le fichier de log, lui, contient tout
    assert len(Path(tmp_path / "j.log").read_text(encoding="utf-8").splitlines()) == 500
