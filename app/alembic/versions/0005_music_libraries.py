"""Bibliothèques musique : type de bibliothèque et cible audio.

- libraries.media_type : video (GPU, échelle de bitrates) ou music (CPU) ;
- libraries.audio_codec / audio_container / audio_bitrate_kbps : cible d'une
  bibliothèque musique (ignorée par les bibliothèques vidéo) ;
- media_files.audio_codec / audio_bitrate_kbps : première piste audio
  sondée (état et statistiques des bibliothèques musique).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUMS: dict[str, list[str]] = {
    "media_type_t": ["video", "music"],
    "audio_codec_t": ["opus", "aac", "mp3"],
    "audio_container_t": ["opus", "ogg", "m4a", "mp3"],
}


def _enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*_ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in _ENUMS.items():
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)

    op.add_column(
        "libraries",
        sa.Column("media_type", _enum("media_type_t"), nullable=False, server_default="video"),
    )
    op.add_column(
        "libraries",
        sa.Column("audio_codec", _enum("audio_codec_t"), nullable=False, server_default="opus"),
    )
    op.add_column(
        "libraries",
        sa.Column(
            "audio_container", _enum("audio_container_t"), nullable=False, server_default="opus"
        ),
    )
    op.add_column(
        "libraries",
        sa.Column(
            "audio_bitrate_kbps", sa.Integer(), nullable=False, server_default=sa.text("128")
        ),
    )
    op.create_check_constraint(
        "ck_audio_bitrate_range", "libraries", "audio_bitrate_kbps BETWEEN 32 AND 512"
    )
    op.add_column("media_files", sa.Column("audio_codec", sa.Text(), nullable=True))
    op.add_column("media_files", sa.Column("audio_bitrate_kbps", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_files", "audio_bitrate_kbps")
    op.drop_column("media_files", "audio_codec")
    op.drop_constraint("ck_audio_bitrate_range", "libraries", type_="check")
    op.drop_column("libraries", "audio_bitrate_kbps")
    op.drop_column("libraries", "audio_container")
    op.drop_column("libraries", "audio_codec")
    op.drop_column("libraries", "media_type")
    bind = op.get_bind()
    for name in _ENUMS:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
