"""Postgres helpers: pooled connections and an idempotent, lock-guarded SQL migration runner."""

from __future__ import annotations

import logging
import time
from importlib import resources

import psycopg
import psycopg.conninfo
import psycopg.sql
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)
_MIGRATION_LOCK = 7_231_001


def wait_for_db(url: str, timeout_s: float = 90) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
                return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1.5)


def ensure_databases(url: str, names: list[str]) -> list[str]:
    """Create sibling databases (e.g. HAPI FHIR's) on the same server; managed Postgres has no init scripts."""
    created = []
    admin_url = psycopg.conninfo.make_conninfo(url, dbname="postgres")
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for name in names:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                conn.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
                created.append(name)
    return created


def make_pool(url: str, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    pool = ConnectionPool(url, min_size=min_size, max_size=max_size, open=False)
    pool.open(wait=True, timeout=60)
    return pool


def migrate(url: str) -> list[str]:
    """Apply rpm_core/migrations/*.sql in lexical order, each exactly once."""
    files = sorted(
        (f for f in resources.files("rpm_core.migrations").iterdir() if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for f in files:
                if f.name in done:
                    continue
                with conn.transaction():
                    conn.execute(f.read_text())
                    conn.execute("INSERT INTO schema_migrations(version) VALUES (%s)", (f.name,))
                log.info("migration applied", extra={"version": f.name})
                applied.append(f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
    return applied
