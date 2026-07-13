"""Schéma initial : enums, libraries, bitrate_rules, gpus, media_files,
jobs, job_progress_snapshots, app_settings.

Revision ID: 0001
Revises:
Create Date: 2026-07-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---------------------------------------------------------------------------
# Types ENUM — créés explicitement, référencés avec create_type=False ensuite
# ---------------------------------------------------------------------------
_ENUMS: dict[str, list[str]] = {
    "codec_t": ["hevc", "av1"],
    "container_t": ["mkv", "mp4"],
    "hdr_policy_t": ["preserve", "tonemap_sdr", "skip_hdr_files"],
    "dv_policy_t": ["convert_to_hdr10", "skip_dv_files"],
    "bucket_t": ["480p", "576p", "720p", "1080p", "1440p", "2160p"],
    "gpu_api_t": ["qsv", "vaapi"],
    "hdr_format_t": ["sdr", "hdr10", "hdr10plus", "dolby_vision", "hlg"],
    "media_status_t": [
        "pending",
        "queued",
        "processing",
        "done",
        "failed",
        "skipped_no_gain",
        "skipped_policy",
        "ignored",
    ],
    "job_state_t": [
        "queued",
        "running",
        "validating",
        "replacing",
        "success",
        "failed",
        "cancelled",
        "skipped_no_gain",
    ],
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    # --- libraries ---------------------------------------------------------
    op.create_table(
        "libraries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("output_codec", _enum("codec_t"), nullable=False, server_default="hevc"),
        sa.Column("container", _enum("container_t"), nullable=False, server_default="mkv"),
        sa.Column("hdr_policy", _enum("hdr_policy_t"), nullable=False, server_default="preserve"),
        sa.Column(
            "dv_policy", _enum("dv_policy_t"), nullable=False, server_default="convert_to_hdr10"
        ),
        sa.Column("date_filter_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "include_extensions",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{mkv,mp4,m2ts,ts,avi,mov,wmv}'::text[]"),
        ),
        sa.Column(
            "exclude_globs",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "min_gain_percent", sa.Numeric(5, 2), nullable=False, server_default=sa.text("5.0")
        ),
        sa.Column("scan_cron", sa.Text(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # --- bitrate_rules -------------------------------------------------------
    op.create_table(
        "bitrate_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("codec", _enum("codec_t"), nullable=False),
        sa.Column("resolution_bucket", _enum("bucket_t"), nullable=False),
        sa.Column("is_hdr", sa.Boolean(), nullable=False),
        sa.Column("target_bitrate_kbps", sa.Integer(), nullable=False),
        sa.Column("max_bitrate_kbps", sa.Integer(), nullable=False),
        sa.Column("bufsize_kbps", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "library_id", "codec", "resolution_bucket", "is_hdr", name="uq_bitrate_rule"
        ),
        sa.CheckConstraint("target_bitrate_kbps > 0", name="ck_target_positive"),
        sa.CheckConstraint("max_bitrate_kbps >= target_bitrate_kbps", name="ck_max_ge_target"),
        sa.CheckConstraint("bufsize_kbps >= target_bitrate_kbps", name="ck_bufsize_ge_target"),
    )
    op.create_index("ix_bitrate_rules_library", "bitrate_rules", ["library_id"])

    # --- gpus ------------------------------------------------------------------
    op.create_table(
        "gpus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("drm_node", sa.Text(), nullable=False, unique=True),
        sa.Column("api", _enum("gpu_api_t"), nullable=False, server_default="qsv"),
        sa.Column("worker_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint("worker_count BETWEEN 1 AND 8", name="ck_worker_count_range"),
    )

    # --- media_files -------------------------------------------------------------
    op.create_table(
        "media_files",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "library_id",
            sa.Integer(),
            sa.ForeignKey("libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False, unique=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mtime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ctime", sa.DateTime(timezone=True), nullable=True),
        sa.Column("btime", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("duration_s", sa.Numeric(12, 3), nullable=True),
        sa.Column("video_codec", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("resolution_bucket", _enum("bucket_t"), nullable=True),
        sa.Column("video_bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("pix_fmt", sa.Text(), nullable=True),
        sa.Column("bit_depth", sa.Integer(), nullable=True),
        sa.Column("color_primaries", sa.Text(), nullable=True),
        sa.Column("color_transfer", sa.Text(), nullable=True),
        sa.Column("color_space", sa.Text(), nullable=True),
        sa.Column("is_hdr", sa.Boolean(), nullable=True),
        sa.Column("hdr_format", _enum("hdr_format_t"), nullable=True),
        sa.Column("dv_profile", sa.Integer(), nullable=True),
        sa.Column("dv_bl_compat_id", sa.Integer(), nullable=True),
        sa.Column("dv_has_el", sa.Boolean(), nullable=True),
        sa.Column("audio_streams", postgresql.JSONB(), nullable=True),
        sa.Column("subtitle_streams", postgresql.JSONB(), nullable=True),
        sa.Column("probe", postgresql.JSONB(), nullable=True),
        sa.Column("status", _enum("media_status_t"), nullable=False, server_default="pending"),
        sa.Column("transcoder_tag", sa.Text(), nullable=True),
        sa.Column("applied_rule_hash", sa.Text(), nullable=True),
        sa.Column("processed_tool_version", sa.Text(), nullable=True),
        sa.Column("manual_rank", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_index("ix_mf_lib_status", "media_files", ["library_id", "status"])
    op.create_index(
        "ix_mf_queue", "media_files", ["status", sa.text("manual_rank NULLS LAST")]
    )
    op.create_index("ix_mf_mtime", "media_files", ["mtime"])

    # --- jobs -------------------------------------------------------------------
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "media_file_id",
            sa.BigInteger(),
            sa.ForeignKey("media_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("library_id", sa.Integer(), nullable=False),
        sa.Column(
            "gpu_id", sa.Integer(), sa.ForeignKey("gpus.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("worker_slot", sa.Integer(), nullable=True),
        sa.Column("state", _enum("job_state_t"), nullable=False, server_default="queued"),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("rule_hash", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_s", sa.Numeric(12, 3), nullable=True),
        # Avant
        sa.Column("src_path", sa.Text(), nullable=True),
        sa.Column("src_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("src_codec", sa.Text(), nullable=True),
        sa.Column("src_bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("src_resolution", sa.Text(), nullable=True),
        sa.Column("src_hdr_format", _enum("hdr_format_t"), nullable=True),
        sa.Column("src_duration_s", sa.Numeric(12, 3), nullable=True),
        sa.Column("src_mtime", sa.DateTime(timezone=True), nullable=True),
        # Après
        sa.Column("dst_path", sa.Text(), nullable=True),
        sa.Column("dst_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("dst_codec", sa.Text(), nullable=True),
        sa.Column("dst_bitrate_kbps", sa.Integer(), nullable=True),
        sa.Column("dst_resolution", sa.Text(), nullable=True),
        sa.Column("dst_hdr_format", _enum("hdr_format_t"), nullable=True),
        sa.Column("dst_duration_s", sa.Numeric(12, 3), nullable=True),
        # Calculés
        sa.Column("bytes_saved", sa.BigInteger(), nullable=True),
        sa.Column("gain_percent", sa.Numeric(6, 2), nullable=True),
        sa.Column("avg_fps", sa.Numeric(8, 2), nullable=True),
        sa.Column("avg_speed", sa.Numeric(6, 2), nullable=True),
        sa.Column("ffmpeg_cmd", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("log_path", sa.Text(), nullable=True),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("validation_report", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_jobs_mf", "jobs", ["media_file_id"])
    op.create_index("ix_jobs_state", "jobs", ["state"])
    op.create_index("ix_jobs_finished", "jobs", [sa.text("finished_at DESC")])
    op.create_index("ix_jobs_library", "jobs", ["library_id"])

    # --- job_progress_snapshots -----------------------------------------------------
    op.create_table(
        "job_progress_snapshots",
        sa.Column(
            "job_id",
            sa.BigInteger(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            primary_key=True,
            server_default=sa.text("now()"),
        ),
        sa.Column("percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("fps", sa.Numeric(8, 2), nullable=True),
        sa.Column("speed", sa.Numeric(6, 2), nullable=True),
        sa.Column("bitrate_kbps", sa.Integer(), nullable=True),
    )

    # --- app_settings ------------------------------------------------------------------
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Trigger updated_at sur libraries (cohérent même en cas d'UPDATE hors ORM)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_libraries_updated_at
        BEFORE UPDATE ON libraries
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_app_settings_updated_at
        BEFORE UPDATE ON app_settings
        FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_app_settings_updated_at ON app_settings")
    op.execute("DROP TRIGGER IF EXISTS trg_libraries_updated_at ON libraries")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.drop_table("app_settings")
    op.drop_table("job_progress_snapshots")
    op.drop_table("jobs")
    op.drop_table("media_files")
    op.drop_table("gpus")
    op.drop_table("bitrate_rules")
    op.drop_table("libraries")
    bind = op.get_bind()
    for name in _ENUMS:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
