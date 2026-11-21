"""baseline: исходная схема MVP (api_keys, usage_logs, telegram_chats)

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("credits_balance", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    op.create_table(
        "usage_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "api_key_id",
            sa.Integer(),
            sa.ForeignKey("api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("endpoint", sa.String(128), nullable=False),
        sa.Column("input_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("schema_name", sa.String(64), nullable=False, server_default="universal_v1"),
        sa.Column("input_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("llm_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cost_credits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
    )
    op.create_index("ix_usage_logs_api_key_id", "usage_logs", ["api_key_id"])
    op.create_index("ix_usage_logs_created_at", "usage_logs", ["created_at"])

    op.create_table(
        "telegram_chats",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("selected_schema_preset", sa.String(64), nullable=True),
        sa.Column("custom_schema", sa.JSON(), nullable=True),
        sa.Column("daily_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("daily_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("telegram_chats")
    op.drop_index("ix_usage_logs_created_at", table_name="usage_logs")
    op.drop_index("ix_usage_logs_api_key_id", table_name="usage_logs")
    op.drop_table("usage_logs")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
