"""Enums applicatifs, miroir exact des types ENUM PostgreSQL.

Les valeurs (str) sont celles persistées en base. Le nom du type PG est
porté par chaque classe via l'attribut non-membre PG_NAME pour garantir
la cohérence entre models.py et les migrations Alembic.
"""

from __future__ import annotations

import enum


class Codec(str, enum.Enum):
    PG_NAME = enum.nonmember("codec_t")

    HEVC = "hevc"
    AV1 = "av1"


class Container(str, enum.Enum):
    PG_NAME = enum.nonmember("container_t")

    MKV = "mkv"
    MP4 = "mp4"


class HdrPolicy(str, enum.Enum):
    PG_NAME = enum.nonmember("hdr_policy_t")

    PRESERVE = "preserve"
    TONEMAP_SDR = "tonemap_sdr"
    SKIP_HDR_FILES = "skip_hdr_files"


class DvPolicy(str, enum.Enum):
    PG_NAME = enum.nonmember("dv_policy_t")

    CONVERT_TO_HDR10 = "convert_to_hdr10"
    SKIP_DV_FILES = "skip_dv_files"


class ResolutionBucket(str, enum.Enum):
    PG_NAME = enum.nonmember("bucket_t")

    P480 = "480p"
    P576 = "576p"
    P720 = "720p"
    P1080 = "1080p"
    P1440 = "1440p"
    P2160 = "2160p"


class GpuApi(str, enum.Enum):
    PG_NAME = enum.nonmember("gpu_api_t")

    QSV = "qsv"
    VAAPI = "vaapi"


class HdrFormat(str, enum.Enum):
    PG_NAME = enum.nonmember("hdr_format_t")

    SDR = "sdr"
    HDR10 = "hdr10"
    HDR10PLUS = "hdr10plus"
    DOLBY_VISION = "dolby_vision"
    HLG = "hlg"


class MediaStatus(str, enum.Enum):
    PG_NAME = enum.nonmember("media_status_t")

    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    SKIPPED_NO_GAIN = "skipped_no_gain"
    SKIPPED_POLICY = "skipped_policy"
    IGNORED = "ignored"


class JobState(str, enum.Enum):
    PG_NAME = enum.nonmember("job_state_t")

    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    REPLACING = "replacing"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED_NO_GAIN = "skipped_no_gain"


def pg_values(e: type[enum.Enum]) -> list[str]:
    """Valeurs persistées en base pour un enum donné."""
    return [m.value for m in e]
