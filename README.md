# Transcodarr

**Self-hosted, never-destructive media transcoding for Jellyfin libraries — a Tdarr replacement.**

Transcodarr continuously re-encodes your media library to HEVC or AV1 on Intel GPUs
(QSV/VAAPI), with per-resolution bitrate targets, full HDR10 / HDR10+ / HLG /
**Dolby Vision** handling, a 9-check post-encode validation gate, and atomic
file replacement with a trash bin. Its core promise: **no code path can ever
destroy or corrupt an original file.**

## Features

- **Hardware encoding** on Intel QSV/VAAPI (Arc, UHD Graphics) — HEVC and AV1,
  10-bit, multi-GPU with per-GPU worker counts, automatic QSV→VAAPI fallback.
- **Bitrate ladders, not CRF guessing**: per-library editable tables of
  target/max/bufsize per resolution bucket (480p→2160p) × SDR/HDR, with
  separate HEVC and AV1 ladders you can switch between without retyping.
- **Dolby Vision done right.** Profile-aware handling (5 / 7 / 8.1 / 8.2 /
  8.4): P5 (IPT-PQ-C2) goes through a mandatory libplacebo gamut conversion —
  never a naive RPU strip that produces the infamous green/purple picture.
  Files that can't be converted safely are skipped, never botched.
- **9 blocking validation checks** before any replacement: exit code, output
  integrity (full CPU decode), duration, stream completeness, size gain,
  HDR colorimetry (BT.2020/PQ flags, MDCV/MaxCLL present, no residual DOVI),
  and a DV anti-color-shift check (SSIM + chroma delta against a
  profile-correct reference decode).
- **Atomic, reversible replacement**: encode next to the file (same
  filesystem → atomic `rename()`), originals go to a trash bin with
  retention (never `rm`), mtime/owner/permissions restored.
- **Anti-loop marker**: a container tag identifies files already processed
  with the current settings — files are only re-queued when *your config*
  changes, or when Radarr/Sonarr upgrades the file (detected by content,
  not name). A mid-encode upgrade guard prevents a stale encode from ever
  overwriting a fresh upgrade.
- **Early no-gain abort**: projected output size is monitored during the
  encode; hopeless jobs are cancelled after ~15% instead of wasting hours
  on a 4K film that won't shrink.
- **Dry-run mode** (global and per-library): real encodes, full history and
  stats, output to a side directory — nothing replaced until you're ready.
- **Web UI** (React, dark theme, mobile-friendly, EN/FR): live dashboard with
  per-worker progress and phase, virtualized queue with drag reordering,
  expandable history with the exact FFmpeg command and searchable live
  logs, statistics (cumulative savings, codec split, gain per bucket,
  failures), full library/GPU/settings management — everything hot-reloaded,
  no restarts.
- **Ops-friendly**: single-folder deployment, single `docker compose up -d`,
  all state in a bind-mounted `data/` you can back up with `tar`, JSON
  config export/import, one-command DB backup/restore scripts, resource
  monitoring (app vs system CPU, RAM, GPU engine usage).

## Requirements

- Linux host with an Intel GPU capable of HEVC/AV1 encode (tested: Arc A770).
  Kernel ≥ 6.2 for Arc, `intel-media-va-driver-non-free`, working `/dev/dri`.
- Docker + Docker Compose.
- PostgreSQL is included in the compose file — nothing else to install.

Check your GPU stack (host **and** inside the container) with:

```bash
./scripts/check_gpu.sh              # on the host
./scripts/check_gpu.sh --container  # inside the running container
```

## Quick start

```bash
git clone https://github.com/literalynn/transcodarr.git /opt/transcodarr
cd /opt/transcodarr
cp .env.example .env
$EDITOR .env       # MEDIA_ROOT, RENDER_GID (getent group render), PUID/PGID, DB password
docker compose up -d --build
```

Open `http://host:8080`. Migrations and seed run automatically; the app
starts in **dry-run** (a full-width banner reminds you) and probes your
GPU with real 5-second test encodes at startup.

Key `.env` settings:

| Variable | Meaning |
|---|---|
| `MEDIA_ROOT` | Common parent of your libraries, mounted **at the same absolute path** inside the container — add/remove libraries from the UI without ever touching the compose file |
| `RENDER_GID` | Numeric GID of the host `render` group (`getent group render`) |
| `PUID`/`PGID` | The app runs as this user and restores file ownership on replacement — use your media owner |
| `DRY_RUN` | Initial global dry-run value (seeded once, then managed in the UI) |

Everything else (bitrate ladders, HDR/DV policies, paths, thresholds,
concurrency, Jellyfin integration) lives in the database and is edited in
the UI, applied hot.

## Recommended burn-in procedure

1. `docker compose up -d --build`, then **GPU & Settings → Test GPU** —
   don't go further until HEVC and AV1 test encodes pass.
2. Build a sandbox from real files of your collection (reflink-aware,
   mtime-preserving):
   ```bash
   ./scripts/make_sample_set.sh --dv --hdr --already-hevc -n 5
   ```
3. Create sandbox libraries in the UI, scan, watch the queue and live logs.
4. Verify outputs with `ffprobe` (codec, color flags, MDCV/MaxCLL, no DOVI
   side data, stream counts — see the validation table below).
5. Disable dry-run **on the sandbox library only**; verify atomic
   replacement, trash content, preserved mtime/permissions; practice a
   rollback (files in trash are plain files — `mv` them back).
6. Create your real libraries with 1 worker, watch the first few jobs,
   then scale workers up and switch validation decode to `sample` for
   throughput.

### Expected results per case

| Input | Expected outcome |
|---|---|
| H.264 SDR | Re-encoded to target codec at the ladder bitrate; audio/subs/chapters/fonts untouched |
| Already-target codec, tagged | Never re-queued while config unchanged |
| HDR10 (4K) | BT.2020/PQ flags + MDCV/MaxCLL present in output, 10-bit |
| Dolby Vision P5 | Valid HDR10 via libplacebo gamut conversion, zero DOVI side data — or `skipped (policy)` if libplacebo lacks DV support; never best-effort |
| Dolby Vision P7 / 8.1 | Base layer re-encoded to clean HDR10 |
| DV 8.2 (SDR base layer) | Treated as SDR, BT.709 flags |
| HDR10+ | Converted to static HDR10 (dynamic metadata can't survive re-encode), reported as such |
| Corrupt file / truncated output | Validation fails, original intact |
| File older than the library date filter | `ignored`, reversible |

Verification one-liners:

```bash
ffprobe -v error -select_streams v:0 -show_entries \
  stream=codec_name,color_primaries,color_transfer,color_space,pix_fmt -of default=nw=1 file.mkv
ffprobe -v error -select_streams v:0 -show_frames -read_intervals "%+#1" file.mkv | grep -c DOVI   # must be 0
ffprobe -v error -show_entries format_tags=TRANSCODER_TAG -of default=nw=1 file.mkv
```

## Operations notes

- **"Date added" semantics**: the per-library date filter uses Jellyfin
  `DateCreated` (if URL + API key are configured) > file birth time >
  mtime. `ctime` is deliberately not used (any chown/chmod rewrites it).
  Note that Jellyfin keeps the original `DateCreated` across quality
  upgrades — if you want upgrades re-encoded, rely on mtime (don't
  configure the Jellyfin integration).
- **Temp strategy**: encodes go to `<file dir>/.transcoder_tmp/` (same
  filesystem → atomic rename). A central tmp dir is only used when it's
  verifiably on the same filesystem (`st_dev`), otherwise it falls back
  automatically.
- **Trash location**: keep the trash on the same filesystem as your media
  (e.g. `${MEDIA_ROOT}/.transcodarr/trash`) so replacements are instant
  renames instead of cross-device copies.
- **Crash safety**: a brutal `docker compose down` mid-encode leaves
  originals untouched; on boot, stale jobs are re-queued and orphan temp
  files purged. The Queue tab also has buttons to recover stuck jobs and
  to re-queue dry-run-only files.
- **MP4 output** is supported but lossy by nature (no font attachments,
  text subs converted to mov_text, image subs fail explicitly). MKV is
  the default for a reason.
- **Backups**: `./scripts/backup.sh` produces a `pg_dump` + JSON config
  archive; `./scripts/restore.sh` restores either. The whole `data/`
  folder is also `tar`-able as-is.
- **Reset**: `./scripts/reset.sh` wipes containers, database, state and
  sandbox (with confirmation) — media libraries are never touched.

## Tests

```bash
docker compose exec app python -m pytest -q
```

~300 tests: pure-function unit coverage of the FFmpeg command builder
(codec × HDR × DV profile matrix), the DV decision matrix, validation
checks, anti-loop logic, atomic replacement on a real filesystem — plus a
software-encoder end-to-end (source generation → encode → 9 validations →
tag round-trip) and, when the database is reachable, a full DB-backed
pipeline test (claim → encode → validate → atomic replace) and an
`alembic check` guarding model/migration drift. No GPU required.

## Architecture (short version)

FastAPI + SQLAlchemy 2 (async) + PostgreSQL 16; asyncio workers claim jobs
with `SELECT … FOR UPDATE SKIP LOCKED`; APScheduler drives per-library scan
crons; WebSockets stream progress and FFmpeg logs to the React 18 + Vite +
Tailwind + shadcn/ui frontend; `jellyfin-ffmpeg` does the heavy lifting,
with `dovi_tool` (RPU L6 extraction) and `mkvpropedit` (container-level
HDR10 metadata + accurate track statistics). Design decisions and their
rationale are journaled in [DECISIONS.md](DECISIONS.md).

## Status

Personal project, shared as-is; battle-tested on a single-server library of
~70k files. Issues and PRs welcome.
