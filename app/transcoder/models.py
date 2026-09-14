"""Modèles ORM SQLAlchemy 2.0 (typés, Mapped[...]).

Miroir exact du schéma créé par la migration 0001. Toute évolution passe
par une nouvelle migration Alembic ; les modèles et le DDL doivent rester
synchrones (vérifié par le check `alembic check` en CI, module tests).
"""

from __future__ import annotations

import datetime
import enum
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from transcoder.enums import (
    Codec,
    Container,
    DvPolicy,
    GpuApi,
    HdrFormat,
    HdrPolicy,
    JobState,
    MediaStatus,
    ResolutionBucket,
    pg_values,
)


def _pg_enum(e: type[enum.Enum]) -> PgEnum:
    """Type ENUM PostgreSQL existant (créé/supprimé par les migrations, jamais par l'ORM)."""
    return PgEnum(
        e,
        name=getattr(e, "PG_NAME"),
        values_callable=pg_values,
        create_type=False,
    )


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[dict[str, Any]]: JSONB,
    }


# ---------------------------------------------------------------------------
# libraries
# ---------------------------------------------------------------------------
class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Pause de la queue pour cette bibliothèque (le scan reste actif)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    output_codec: Mapped[Codec] = mapped_column(
        _pg_enum(Codec), nullable=False, server_default=text("'hevc'")
    )
    container: Mapped[Container] = mapped_column(
        _pg_enum(Container), nullable=False, server_default=text("'mkv'")
    )
    hdr_policy: Mapped[HdrPolicy] = mapped_column(
        _pg_enum(HdrPolicy), nullable=False, server_default=text("'preserve'")
    )
    dv_policy: Mapped[DvPolicy] = mapped_column(
        _pg_enum(DvPolicy), nullable=False, server_default=text("'convert_to_hdr10'")
    )

    date_filter_after: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    include_extensions: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        nullable=False,
        server_default=text("'{mkv,mp4,m2ts,ts,avi,mov,wmv}'::text[]"),
    )
    exclude_globs: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )

    min_gain_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("5.0")
    )
    scan_cron: Mapped[str | None] = mapped_column(Text, nullable=True)

    # NULL = hérite du réglage global dry_run_global (app_settings) — §9.4
    dry_run: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=datetime.datetime.now,
    )

    bitrate_rules: Mapped[list[BitrateRule]] = relationship(
        back_populates="library", cascade="all, delete-orphan"
    )
    media_files: Mapped[list[MediaFile]] = relationship(
        back_populates="library", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# bitrate_rules — l'échelle de qualité, 2 jeux (HEVC + AV1) par bibliothèque
# ---------------------------------------------------------------------------
class BitrateRule(Base):
    __tablename__ = "bitrate_rules"
    __table_args__ = (
        UniqueConstraint(
            "library_id", "codec", "resolution_bucket", "is_hdr", name="uq_bitrate_rule"
        ),
        CheckConstraint("target_bitrate_kbps > 0", name="ck_target_positive"),
        CheckConstraint("max_bitrate_kbps >= target_bitrate_kbps", name="ck_max_ge_target"),
        CheckConstraint("bufsize_kbps >= target_bitrate_kbps", name="ck_bufsize_ge_target"),
        Index("ix_bitrate_rules_library", "library_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False
    )
    codec: Mapped[Codec] = mapped_column(_pg_enum(Codec), nullable=False)
    resolution_bucket: Mapped[ResolutionBucket] = mapped_column(
        _pg_enum(ResolutionBucket), nullable=False
    )
    is_hdr: Mapped[bool] = mapped_column(Boolean, nullable=False)
    target_bitrate_kbps: Mapped[int] = mapped_column(Integer, nullable=False)
    max_bitrate_kbps: Mapped[int] = mapped_column(Integer, nullable=False)
    bufsize_kbps: Mapped[int] = mapped_column(Integer, nullable=False)

    library: Mapped[Library] = relationship(back_populates="bitrate_rules")


# ---------------------------------------------------------------------------
# gpus
# ---------------------------------------------------------------------------
class Gpu(Base):
    __tablename__ = "gpus"
    __table_args__ = (
        CheckConstraint("worker_count BETWEEN 1 AND 8", name="ck_worker_count_range"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    drm_node: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    api: Mapped[GpuApi] = mapped_column(
        _pg_enum(GpuApi), nullable=False, server_default=text("'qsv'")
    )
    worker_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


# ---------------------------------------------------------------------------
# media_files — état courant de chaque fichier connu
# ---------------------------------------------------------------------------
class MediaFile(Base):
    __tablename__ = "media_files"
    __table_args__ = (
        Index("ix_mf_lib_status", "library_id", "status"),
        Index("ix_mf_queue", "status", text("manual_rank NULLS LAST")),
        Index("ix_mf_mtime", "mtime"),
        Index("ix_mf_gain", text("estimated_gain_bytes DESC NULLS LAST")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    library_id: Mapped[int] = mapped_column(
        ForeignKey("libraries.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mtime: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    ctime: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # btime (statx) quand le filesystem l'expose — plus fiable pour le filtre
    # « ajouté après le … » (§5.1, arbitrage 3 du plan)
    btime: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # --- Sonde ffprobe ----------------------------------------------------
    duration_s: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    video_codec: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    resolution_bucket: Mapped[ResolutionBucket | None] = mapped_column(_pg_enum(ResolutionBucket))
    video_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    pix_fmt: Mapped[str | None] = mapped_column(Text)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    color_primaries: Mapped[str | None] = mapped_column(Text)
    color_transfer: Mapped[str | None] = mapped_column(Text)
    color_space: Mapped[str | None] = mapped_column(Text)
    is_hdr: Mapped[bool | None] = mapped_column(Boolean)
    hdr_format: Mapped[HdrFormat | None] = mapped_column(_pg_enum(HdrFormat))

    # --- Dolby Vision (§5.3.1) ---------------------------------------------
    dv_profile: Mapped[int | None] = mapped_column(Integer)
    dv_bl_compat_id: Mapped[int | None] = mapped_column(Integer)
    dv_has_el: Mapped[bool | None] = mapped_column(Boolean)

    audio_streams: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    subtitle_streams: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # --- État --------------------------------------------------------------
    status: Mapped[MediaStatus] = mapped_column(
        _pg_enum(MediaStatus), nullable=False, server_default=text("'pending'")
    )
    # Tag conteneur lu dans le fichier (marqueur anti-boucle §5.7)
    transcoder_tag: Mapped[str | None] = mapped_column(Text)
    # Hash de la règle (codec inclus) appliquée lors du dernier traitement réussi
    applied_rule_hash: Mapped[str | None] = mapped_column(Text)
    processed_tool_version: Mapped[str | None] = mapped_column(Text)
    # NULL = ordre automatique ; sinon priorité manuelle (drag & drop §7.2)
    manual_rank: Mapped[int | None] = mapped_column(Integer)
    # Gain potentiel estimé au scan : critère d'ordonnancement §5.2
    estimated_gain_bytes: Mapped[int | None] = mapped_column(BigInteger)
    # Backoff des retries : pas de nouvelle tentative avant cette date (§5.2)
    next_attempt_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # --- Suivi des échecs et replis (migration 0004) --------------------------
    # Catégorie du dernier échec (core/failures.py), NULL si aucun
    failure_category: Mapped[str | None] = mapped_column(Text)
    # Tentatives de la série en cours (remis à 0 : succès, relance, règle changée)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Stratégies déjà tentées dans la série en cours (core/strategy.py)
    strategies_tried: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    # Stratégie de la prochaine tentative (NULL = standard)
    next_strategy: Mapped[str | None] = mapped_column(Text)

    library: Mapped[Library] = relationship(back_populates="media_files")
    jobs: Mapped[list[Job]] = relationship(
        back_populates="media_file", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# jobs — une ligne par tentative de réencodage (historique)
# ---------------------------------------------------------------------------
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_mf", "media_file_id"),
        Index("ix_jobs_state", "state"),
        Index("ix_jobs_finished", text("finished_at DESC")),
        Index("ix_jobs_library", "library_id"),
        Index("ix_jobs_failure_category", "failure_category"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    media_file_id: Mapped[int] = mapped_column(
        ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False
    )
    library_id: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_id: Mapped[int | None] = mapped_column(ForeignKey("gpus.id", ondelete="SET NULL"))
    worker_slot: Mapped[int | None] = mapped_column(Integer)

    state: Mapped[JobState] = mapped_column(
        _pg_enum(JobState), nullable=False, server_default=text("'queued'")
    )
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rule_hash: Mapped[str] = mapped_column(Text, nullable=False)

    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    # --- Avant ---------------------------------------------------------------
    src_path: Mapped[str | None] = mapped_column(Text)
    src_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    src_codec: Mapped[str | None] = mapped_column(Text)
    src_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    src_resolution: Mapped[str | None] = mapped_column(Text)
    src_hdr_format: Mapped[HdrFormat | None] = mapped_column(_pg_enum(HdrFormat))
    src_duration_s: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    # mtime original, stocké AVANT écrasement pour restauration (§5.6)
    src_mtime: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Après ---------------------------------------------------------------
    dst_path: Mapped[str | None] = mapped_column(Text)
    dst_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    dst_codec: Mapped[str | None] = mapped_column(Text)
    dst_bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    dst_resolution: Mapped[str | None] = mapped_column(Text)
    dst_hdr_format: Mapped[HdrFormat | None] = mapped_column(_pg_enum(HdrFormat))
    dst_duration_s: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    # --- Calculés --------------------------------------------------------------
    bytes_saved: Mapped[int | None] = mapped_column(BigInteger)
    gain_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    avg_fps: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    avg_speed: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))

    # Emplacement de l'original en corbeille après remplacement (rollback §9.5.8)
    trashed_path: Mapped[str | None] = mapped_column(Text)

    ffmpeg_cmd: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(Text)
    log_tail: Mapped[str | None] = mapped_column(Text)
    validation_report: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # --- Diagnostic (migration 0004) --------------------------------------------
    # N° de tentative dans la série du fichier (1 = premier essai)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    # Stratégie utilisée (standard / tolerant / safe_streams / alt_api / preflight)
    strategy: Mapped[str | None] = mapped_column(Text)
    failure_category: Mapped[str | None] = mapped_column(Text)
    # Lignes d'erreur significatives (log FFmpeg filtré, stderr de validation)
    failure_detail: Mapped[str | None] = mapped_column(Text)
    # Pré-vol, plan de pistes, décision de repli
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    media_file: Mapped[MediaFile] = relationship(back_populates="jobs")
    progress_snapshots: Mapped[list[JobProgressSnapshot]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# job_progress_snapshots — échantillonnage pour les graphes de charge
# ---------------------------------------------------------------------------
class JobProgressSnapshot(Base):
    __tablename__ = "job_progress_snapshots"

    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=text("now()")
    )
    percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    fps: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    speed: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer)

    job: Mapped[Job] = relationship(back_populates="progress_snapshots")


# ---------------------------------------------------------------------------
# app_settings — clé/valeur JSONB, rechargé à chaud
# ---------------------------------------------------------------------------
class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    # JSONB : scalaire, objet ou tableau selon la clé
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=datetime.datetime.now,
    )
