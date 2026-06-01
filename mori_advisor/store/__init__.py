"""Store factory — selects SQLiteStore or PostgresStore at runtime.

MORI_DATABASE_URL not set (or empty)  → SQLiteStore (default)
MORI_DATABASE_URL=postgresql://...    → PostgresStore

Usage:
    from mori_advisor.store import get_store
    store = get_store()          # uses MORI_ADVISOR_DATA env var for path
    store = get_store(db_path)   # explicit path (SQLite only)
"""

from __future__ import annotations

import os
from pathlib import Path

from .base import BaseStore


def get_store(db_path: str | Path | None = None) -> BaseStore:
    """Return the configured store backend.

    PostgresStore is returned when MORI_DATABASE_URL is set to a
    postgresql:// URL. All other cases return SQLiteStore.

    The asyncpg import is deferred inside PostgresStore.__init__ so
    SQLite-only deployments never require asyncpg to be installed.
    """
    url = os.environ.get("MORI_DATABASE_URL", "").strip()
    if url.startswith(("postgresql://", "postgres://")):
        from .postgres_store import PostgresStore
        return PostgresStore(url)

    from .sqlite_store import SQLiteStore
    if db_path is None:
        data_dir = os.environ.get("MORI_ADVISOR_DATA", "/data/mori-advisor")
        db_path = Path(data_dir) / "memories.db"
    return SQLiteStore(db_path)


__all__ = ["BaseStore", "get_store"]
