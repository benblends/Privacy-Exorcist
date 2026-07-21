"""
PrivacyExorcist SQLite persistence layer.

SPEC-001 §3.4 schema + §5 Phase 2 CRUD operations.
Uses sqlite3 stdlib — zero external dependencies.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from privacy_exorcist.models import BrokerRecord, BrokerState


# ── Schema ──────────────────────────────────────────────────────────────────

CREATE_BROKER_LEDGER = """
CREATE TABLE IF NOT EXISTS broker_ledger (
    broker_id          TEXT PRIMARY KEY,
    current_status     TEXT NOT NULL DEFAULT 'QUEUED',
    last_run_timestamp TEXT,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    captcha_solves     INTEGER NOT NULL DEFAULT 0,
    error_log          TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_RUN_HISTORY = """
CREATE TABLE IF NOT EXISTS run_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    broker_id          TEXT NOT NULL,
    run_started        TEXT NOT NULL,
    run_completed      TEXT,
    outcome            TEXT,
    duration_seconds   REAL,
    FOREIGN KEY (broker_id) REFERENCES broker_ledger(broker_id)
);
"""


class Database:
    """SQLite persistence for broker state and run history.

    Thread-safe at the connection level — each call opens and closes
    a fresh connection via the _connection context manager.
    """

    def __init__(self, db_path: str = ":memory:"):
        self._db_path = db_path

    # ── Connection Management ───────────────────────────────────────────

    @contextmanager
    def _connection(self):
        """Yield a sqlite3.Connection. Auto-closes on context exit.

        Enables WAL mode for better concurrent-read behaviour,
        and enforces foreign keys.
        """
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Schema Migration ─────────────────────────────────────────────────

    def migrate(self) -> None:
        """Create tables if they don't exist. Idempotent."""
        with self._connection() as conn:
            conn.execute(CREATE_BROKER_LEDGER)
            conn.execute(CREATE_RUN_HISTORY)

    # ── Broker CRUD ──────────────────────────────────────────────────────

    def upsert_broker(
        self,
        broker_id: str,
        status: str,
        error_log: Optional[str] = None,
    ) -> None:
        """Insert or update a broker row in the ledger."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT broker_id FROM broker_ledger WHERE broker_id = ?",
                (broker_id,),
            )
            if cursor.fetchone():
                conn.execute(
                    """UPDATE broker_ledger
                       SET current_status = ?,
                           error_log = ?,
                           last_run_timestamp = ?,
                           updated_at = ?
                       WHERE broker_id = ?""",
                    (status, error_log, now, now, broker_id),
                )
            else:
                conn.execute(
                    """INSERT INTO broker_ledger
                       (broker_id, current_status, error_log,
                        last_run_timestamp, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (broker_id, status, error_log, now, now, now),
                )

    def get_broker(self, broker_id: str) -> Optional[BrokerRecord]:
        """Fetch a single broker record. Returns None if not found."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM broker_ledger WHERE broker_id = ?",
                (broker_id,),
            ).fetchone()
            if row is None:
                return None
            return BrokerRecord(
                broker_id=row["broker_id"],
                current_status=BrokerState(row["current_status"]),
                last_run_timestamp=row["last_run_timestamp"],
                retry_count=row["retry_count"],
                captcha_solves=row["captcha_solves"],
                error_log=row["error_log"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def get_all_brokers(self) -> list[BrokerRecord]:
        """Fetch every broker in the ledger."""
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM broker_ledger").fetchall()
        return [
            BrokerRecord(
                broker_id=r["broker_id"],
                current_status=BrokerState(r["current_status"]),
                last_run_timestamp=r["last_run_timestamp"],
                retry_count=r["retry_count"],
                captcha_solves=r["captcha_solves"],
                error_log=r["error_log"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # ── Run History ──────────────────────────────────────────────────────

    def log_run(
        self,
        broker_id: str,
        outcome: str,
        duration_seconds: float,
    ) -> Optional[int]:
        """Insert a row into run_history. Returns the new row id."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as conn:
            cursor = conn.execute(
                """INSERT INTO run_history
                   (broker_id, run_started, run_completed, outcome, duration_seconds)
                   VALUES (?, ?, ?, ?, ?)""",
                (broker_id, now, now, outcome, duration_seconds),
            )
            return cursor.lastrowid

    # ── Counters ─────────────────────────────────────────────────────────

    def increment_retry(self, broker_id: str) -> int:
        """Increment retry_count and return the new value."""
        with self._connection() as conn:
            conn.execute(
                """UPDATE broker_ledger
                   SET retry_count = retry_count + 1,
                       updated_at = ?
                   WHERE broker_id = ?""",
                (datetime.now(timezone.utc).isoformat(), broker_id),
            )
            row = conn.execute(
                "SELECT retry_count FROM broker_ledger WHERE broker_id = ?",
                (broker_id,),
            ).fetchone()
            return row["retry_count"] if row else 0

    def increment_captcha_solve(self, broker_id: str) -> int:
        """Increment captcha_solves and return the new value."""
        with self._connection() as conn:
            conn.execute(
                """UPDATE broker_ledger
                   SET captcha_solves = captcha_solves + 1,
                       updated_at = ?
                   WHERE broker_id = ?""",
                (datetime.now(timezone.utc).isoformat(), broker_id),
            )
            row = conn.execute(
                "SELECT captcha_solves FROM broker_ledger WHERE broker_id = ?",
                (broker_id,),
            ).fetchone()
            return row["captcha_solves"] if row else 0
