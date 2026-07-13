"""jobs.trashed_path : emplacement de l'original en corbeille (§5.6).

Nécessaire pour le rollback documenté (§9.5.8) : savoir où chaque original
remplacé est parti sans dépendre du parsing des logs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("trashed_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "trashed_path")
