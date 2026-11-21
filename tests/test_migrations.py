"""Миграции Alembic применяются к чистой базе и откатываются."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
VENV_PYTHON = REPO / ".venv" / "bin" / "python"

BASELINE_TABLES = {"api_keys", "usage_logs", "telegram_chats"}
FULL_TABLES = BASELINE_TABLES | {
    "credit_transactions", "credit_holds", "idempotency_keys", "admin_audit_logs",
    "webhook_endpoints", "webhook_deliveries", "jobs", "job_items",
    "credit_packages", "payments", "invoices",
    "organizations", "users", "memberships", "sessions",
    "customers",  # 3.0
}


def test_alembic_upgrade_2_0_data_to_3_0(tmp_path: Path) -> None:
    """Миграция 3.0 применяется к базе 2.0 с данными без потерь."""
    import sqlite3

    db = tmp_path / "v20.db"
    assert _run_alembic(db, "upgrade", "0002_commercial").returncode == 0
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO api_keys (name, key_hash, key_prefix, is_active, credits_balance,"
        " created_at) VALUES ('legacy', 'h1', 'pj_L', 1, 777, '2026-11-20 00:00:00')"
    )
    conn.execute(
        "INSERT INTO credit_packages (code, name, credits, price_amount, currency, active)"
        " VALUES ('growth', 'Growth', 6000, 149.0, 'USD', 1)"
    )
    conn.commit()
    conn.close()

    result = _run_alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr

    conn = sqlite3.connect(db)
    balance = conn.execute(
        "SELECT credits_balance FROM api_keys WHERE name='legacy'"
    ).fetchone()[0]
    assert balance == 777
    stars = conn.execute(
        "SELECT stars_amount FROM credit_packages WHERE code='growth'"
    ).fetchone()[0]
    assert stars == 0  # сид заполнит реальные цены отдельно
    customers_exists = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='customers'"
    ).fetchone()[0]
    assert customers_exists == 1
    conn.close()


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
    }
    return subprocess.run(
        [str(VENV_PYTHON), "-m", "alembic", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _tables(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()


def test_alembic_upgrade_head_on_clean_db(tmp_path: Path) -> None:
    db = tmp_path / "clean.db"
    result = _run_alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr + result.stdout
    tables = _tables(db)
    missing = FULL_TABLES - tables
    assert not missing, f"отсутствуют таблицы: {missing}"


def test_alembic_upgrade_baseline_then_head(tmp_path: Path) -> None:
    db = tmp_path / "staged.db"
    first = _run_alembic(db, "upgrade", "0001_baseline")
    assert first.returncode == 0, first.stderr
    tables = _tables(db)
    assert BASELINE_TABLES <= tables
    assert "credit_holds" not in tables

    second = _run_alembic(db, "upgrade", "head")
    assert second.returncode == 0, second.stderr
    assert FULL_TABLES <= _tables(db)


def test_alembic_downgrade_to_baseline(tmp_path: Path) -> None:
    db = tmp_path / "down.db"
    assert _run_alembic(db, "upgrade", "head").returncode == 0
    result = _run_alembic(db, "downgrade", "0001_baseline")
    assert result.returncode == 0, result.stderr
    tables = _tables(db)
    assert "credit_holds" not in tables
    assert BASELINE_TABLES <= tables


def test_alembic_single_revision_creation(tmp_path: Path) -> None:
    """make revision генерирует файл с корректными якорями."""
    versions_dir = REPO / "migrations" / "versions"
    result = _run_alembic(
        tmp_path / "rev.db",
        "revision",
        "-m",
        "smoke test revision",
        "--rev-id",
        "zzzz_smoke",
    )
    assert result.returncode == 0, result.stderr
    created = versions_dir / "zzzz_smoke_smoke_test_revision.py"
    assert created.exists()
    created.unlink()

def test_alembic_schema_matches_models(tmp_path: Path) -> None:
    """Регрессия дрейфа: схема из миграций == схема из моделей (по колонкам).

    Ловит случай production-Postgres 21.11.2026: usage_logs.meta и
    credit_packages.created_at были в моделях, но не в миграциях.
    """
    import sqlite3

    from sqlalchemy import create_engine

    from app.models import Base

    db = tmp_path / "drift.db"
    assert _run_alembic(db, "upgrade", "head").returncode == 0

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    model_tables = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        mig_tables = {
            name: {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
            for (name,) in rows
        }
    finally:
        conn.close()

    drift: list[str] = []
    for name, model_cols in model_tables.items():
        mig_cols = mig_tables.get(name)
        if mig_cols is None:
            drift.append(f"{name}: таблица отсутствует в миграциях")
            continue
        missing = model_cols - mig_cols
        if missing:
            drift.append(f"{name}: нет колонок {sorted(missing)}")
    assert not drift, f"Дрейф схемы (миграции отстают от моделей): {drift}"

