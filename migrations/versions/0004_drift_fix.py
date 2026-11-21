"""fix schema drift: usage_logs.meta, credit_packages.created_at

Дрейф «модели vs миграции», выявленный на production-Postgres:
- usage_logs.meta (JSON) — модель 2.0 писала его, но миграций не было;
  без него INSERT usage_logs с мета-данными падает (UndefinedColumn).
- credit_packages.created_at — есть в модели, отсутствовал в миграции 0002.

Regression-тест tests/test_migrations.py::test_alembic_schema_matches_models
гарантирует отсутствие нового дрейфа.

Revision ID: 0004_drift_fix
Revises: 0003_launch
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_drift_fix"
down_revision: str | None = "0003_launch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usage_logs") as batch:
        batch.add_column(sa.Column("meta", sa.JSON(), nullable=True))
    with op.batch_alter_table("credit_packages") as batch:
        batch.add_column(
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("credit_packages") as batch:
        batch.drop_column("created_at")
    with op.batch_alter_table("usage_logs") as batch:
        batch.drop_column("meta")
