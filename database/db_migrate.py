"""
db_migrate.py — lightweight ALTER TABLE migration for SQLite.

Called once at startup (inside init_db) to add new columns introduced
after the initial schema was created.  Safe to run repeatedly; each
ALTER is skipped if the column already exists.
"""

from sqlalchemy import text
from loguru import logger


def _existing_columns(conn, table: str) -> set:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}  # column name is index 1


def run_migrations(engine) -> None:
    """Add any missing columns to existing tables."""
    with engine.connect() as conn:
        # ── trades table ──────────────────────────────────────────────────
        existing = _existing_columns(conn, "trades")

        pending = []

        if "owner_id" not in existing:
            pending.append(
                "ALTER TABLE trades ADD COLUMN owner_id INTEGER REFERENCES owners(id)"
            )

        if "strategy" not in existing:
            pending.append(
                "ALTER TABLE trades ADD COLUMN strategy VARCHAR(100)"
            )

        for sql in pending:
            try:
                conn.execute(text(sql))
                logger.info(f"Migration applied: {sql[:70]}…")
            except Exception as exc:
                logger.warning(f"Migration skipped ({exc}): {sql[:70]}…")

        if not pending:
            logger.debug("DB schema is up to date — no migrations needed.")

        conn.commit()
