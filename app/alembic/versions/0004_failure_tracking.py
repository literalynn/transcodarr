"""Suivi des échecs, stratégies de repli et diagnostics.

- jobs.attempt / strategy / failure_category / failure_detail / diagnostics :
  chaque tentative dit AVEC QUOI elle a été faite et POURQUOI elle a échoué ;
- media_files.failure_category / attempt_count / strategies_tried /
  next_strategy : état de la série de tentatives d'un fichier, qui pilote
  le repli automatique et empêche la re-tentative infinie d'un échec
  définitif (boucle observée : jusqu'à 161 tentatives par fichier).

Catégories et stratégies en TEXT (pas d'ENUM PostgreSQL) : la liste
évolue avec les versions sans migration de type.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1"))
    )
    op.add_column("jobs", sa.Column("strategy", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("failure_category", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("failure_detail", sa.Text(), nullable=True))
    op.add_column(
        "jobs", sa.Column("diagnostics", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_index("ix_jobs_failure_category", "jobs", ["failure_category"])

    op.add_column("media_files", sa.Column("failure_category", sa.Text(), nullable=True))
    op.add_column(
        "media_files",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "media_files",
        sa.Column(
            "strategies_tried",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.add_column("media_files", sa.Column("next_strategy", sa.Text(), nullable=True))

    # Historique : numéro de tentative reconstitué une fois pour toutes. Une
    # série de tentatives d'un fichier se termine à chaque succès ou « sans
    # gain » ; les jobs sont numérotés dans leur série (1 = premier essai).
    op.execute(
        """
        WITH ordered AS (
            SELECT id, media_file_id,
                   COALESCE(SUM(CASE WHEN state IN ('success', 'skipped_no_gain') THEN 1 ELSE 0 END)
                       OVER (PARTITION BY media_file_id ORDER BY id
                             ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS series
            FROM jobs
        ),
        numbered AS (
            SELECT id, ROW_NUMBER() OVER (PARTITION BY media_file_id, series ORDER BY id) AS n
            FROM ordered
        )
        UPDATE jobs SET attempt = numbered.n
        FROM numbered
        WHERE jobs.id = numbered.id AND numbered.n > 1
        """
    )


def downgrade() -> None:
    op.drop_column("media_files", "next_strategy")
    op.drop_column("media_files", "strategies_tried")
    op.drop_column("media_files", "attempt_count")
    op.drop_column("media_files", "failure_category")
    op.drop_index("ix_jobs_failure_category", table_name="jobs")
    op.drop_column("jobs", "diagnostics")
    op.drop_column("jobs", "failure_detail")
    op.drop_column("jobs", "failure_category")
    op.drop_column("jobs", "strategy")
    op.drop_column("jobs", "attempt")
