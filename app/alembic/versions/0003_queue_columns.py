"""Colonnes d'ordonnancement de la queue (§5.2).

- libraries.paused : pause par bibliothèque (distincte de enabled qui
  gouverne aussi le scan) ;
- media_files.estimated_gain_bytes : gain potentiel estimé au scan
  ((bitrate source − bitrate cible) × durée), critère d'ordonnancement ;
- media_files.next_attempt_at : backoff des retries (§5.2).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "media_files", sa.Column("estimated_gain_bytes", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "media_files",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_mf_gain", "media_files", [sa.text("estimated_gain_bytes DESC NULLS LAST")]
    )


def downgrade() -> None:
    op.drop_index("ix_mf_gain", table_name="media_files")
    op.drop_column("media_files", "next_attempt_at")
    op.drop_column("media_files", "estimated_gain_bytes")
    op.drop_column("libraries", "paused")
