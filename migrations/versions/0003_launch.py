"""launch hardening 3.0: клиенты, Stars-пакеты, TTL холдов, констрейнты

Revision ID: 0003_launch
Revises: 0002_commercial
Create Date: 2026-11-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_launch"
down_revision: str | None = "0002_commercial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Клиенты (минимальный self-serve через Telegram) ---
    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_customers_telegram_user_id", "customers", ["telegram_user_id"], unique=True)

    with op.batch_alter_table("api_keys") as batch:
        batch.add_column(
            sa.Column(
                "customer_id",
                sa.Integer(),
                sa.ForeignKey("customers.id", ondelete="SET NULL", name="fk_api_keys_customer_id"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_api_keys_customer", ["customer_id"])
        # Инвариант 3.0: баланс не уходит ниже нуля на уровне БД
        batch.create_check_constraint("ck_api_keys_balance_nonnegative", "credits_balance >= 0")

    # --- Пакеты: колонки Telegram Stars ---
    with op.batch_alter_table("credit_packages") as batch:
        batch.add_column(sa.Column("title", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column("stars_amount", sa.Integer(), nullable=False, server_default="0")
        )
        batch.alter_column("currency", existing_type=sa.String(8), server_default="XTR")
        batch.create_check_constraint("ck_packages_credits_positive", "credits > 0")
        batch.create_check_constraint("ck_packages_stars_nonnegative", "stars_amount >= 0")

    # --- Холды: TTL и heartbeat ---
    with op.batch_alter_table("credit_holds") as batch:
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_credit_holds_active_expiry", ["status", "expires_at"])

    # --- Платежи: статус received/processing допустим строковым полем (без DDL),
    #     но добавим метку времени попыток зачисления для сверки ---
    with op.batch_alter_table("payments") as batch:
        batch.add_column(sa.Column("credited_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("problem", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("payments") as batch:
        batch.drop_column("problem")
        batch.drop_column("refunded_at")
        batch.drop_column("credited_at")
    with op.batch_alter_table("credit_holds") as batch:
        batch.drop_index("ix_credit_holds_active_expiry")
        batch.drop_column("last_heartbeat_at")
        batch.drop_column("expires_at")
    with op.batch_alter_table("credit_packages") as batch:
        batch.drop_constraint("ck_packages_stars_nonnegative", type_="check")
        batch.drop_constraint("ck_packages_credits_positive", type_="check")
        batch.alter_column("currency", existing_type=sa.String(8), server_default="USD")
        batch.drop_column("stars_amount")
        batch.drop_column("title")
    with op.batch_alter_table("api_keys") as batch:
        batch.drop_constraint("ck_api_keys_balance_nonnegative", type_="check")
        batch.drop_index("ix_api_keys_customer")
        batch.drop_column("is_primary")
        batch.drop_column("customer_id")
    op.drop_index("ix_customers_telegram_user_id", table_name="customers")
    op.drop_table("customers")
