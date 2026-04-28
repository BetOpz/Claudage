"""
database.py - Persistent storage for Claude Code usage history.

Stores per-snapshot and per-session records in a local SQLite database so
the optimization engine can rank time slots by historical burn rate.
"""

import csv
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionRecord:
    start_time: datetime
    end_time: datetime
    duration_minutes: float
    total_tokens: int
    input_tokens: int
    output_tokens: int
    cache_tokens: int
    cost_usd: float
    burn_rate_per_min: float
    session_limit: int
    pct_used: float
    id: Optional[int] = None


class UsageDatabase:
    """SQLite-backed store for usage snapshots and completed session records."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time          TEXT    NOT NULL,
                    end_time            TEXT    NOT NULL,
                    duration_minutes    REAL    NOT NULL,
                    total_tokens        INTEGER NOT NULL,
                    input_tokens        INTEGER DEFAULT 0,
                    output_tokens       INTEGER DEFAULT 0,
                    cache_tokens        INTEGER DEFAULT 0,
                    cost_usd            REAL    DEFAULT 0.0,
                    burn_rate_per_min   REAL    NOT NULL,
                    session_limit       INTEGER NOT NULL,
                    pct_used            REAL    NOT NULL,
                    created_at          TEXT    DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_start
                    ON sessions (start_time);

                CREATE TABLE IF NOT EXISTS hourly_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_time   TEXT    NOT NULL,
                    day_of_week     INTEGER NOT NULL,
                    hour_of_day     INTEGER NOT NULL,
                    tokens          INTEGER NOT NULL,
                    burn_rate       REAL    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_time
                    ON hourly_snapshots (snapshot_time);

                CREATE TABLE IF NOT EXISTS multiplier_log (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    logged_at           TEXT    NOT NULL,
                    model               TEXT    NOT NULL DEFAULT '',
                    session_tokens      INTEGER NOT NULL,
                    tokens_delta        INTEGER NOT NULL,
                    session_pct         REAL    NOT NULL,
                    session_pct_delta   REAL    NOT NULL,
                    session_multiplier  REAL    NOT NULL,
                    weekly_tokens       INTEGER NOT NULL,
                    weekly_pct          REAL    NOT NULL,
                    weekly_pct_delta    REAL    NOT NULL,
                    weekly_multiplier   REAL    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_multiplier_log_time
                    ON multiplier_log (logged_at);
            """)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Write operations ──────────────────────────────────────────────────────

    def save_snapshot(self, tokens: int, burn_rate: float):
        """Record a real-time metric snapshot for later optimization analysis."""
        if burn_rate <= 0:
            return
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO hourly_snapshots
                    (snapshot_time, day_of_week, hour_of_day, tokens, burn_rate)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now.isoformat(), now.weekday(), now.hour, tokens, burn_rate),
            )

    def save_multiplier(self, session_tokens: int, tokens_delta: int,
                        session_pct: float, session_pct_delta: float, session_multiplier: float,
                        weekly_tokens: int, weekly_pct: float,
                        weekly_pct_delta: float, weekly_multiplier: float,
                        model: str = ""):
        """Log observed token-cost multipliers for both the 5hr and weekly windows."""
        now = datetime.now(timezone.utc)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO multiplier_log
                    (logged_at, model, session_tokens, tokens_delta,
                     session_pct, session_pct_delta, session_multiplier,
                     weekly_tokens, weekly_pct, weekly_pct_delta, weekly_multiplier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now.isoformat(), model, session_tokens, tokens_delta,
                 session_pct, session_pct_delta, session_multiplier,
                 weekly_tokens, weekly_pct, weekly_pct_delta, weekly_multiplier),
            )

    def get_recent_multipliers(self, hours_back: int = 24) -> List[dict]:
        """Return multiplier log entries from the last N hours, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM multiplier_log
                WHERE logged_at >= datetime('now', ? || ' hours')
                ORDER BY logged_at DESC
                """,
                (f"-{hours_back}",),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_session(self, record: SessionRecord) -> int:
        """Persist a completed session; returns the new row id."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions
                    (start_time, end_time, duration_minutes, total_tokens,
                     input_tokens, output_tokens, cache_tokens, cost_usd,
                     burn_rate_per_min, session_limit, pct_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.start_time.isoformat(),
                    record.end_time.isoformat(),
                    record.duration_minutes,
                    record.total_tokens,
                    record.input_tokens,
                    record.output_tokens,
                    record.cache_tokens,
                    record.cost_usd,
                    record.burn_rate_per_min,
                    record.session_limit,
                    record.pct_used,
                ),
            )
            return cur.lastrowid

    # ── Read operations ───────────────────────────────────────────────────────

    def get_hourly_stats(self, days_back: int = 90) -> List[dict]:
        """
        Return average burn rates grouped by (day_of_week, hour_of_day).

        Only includes slots with at least 2 samples so single outliers don't
        dominate the rankings.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    day_of_week,
                    hour_of_day,
                    AVG(burn_rate)  AS avg_burn_rate,
                    COUNT(*)        AS sample_count
                FROM hourly_snapshots
                WHERE snapshot_time >= datetime('now', ? || ' days')
                GROUP BY day_of_week, hour_of_day
                HAVING sample_count >= 2
                ORDER BY avg_burn_rate ASC
                """,
                (f"-{days_back}",),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_hourly_multiplier_stats(self, days_back: int = 90) -> List[dict]:
        """
        Return average session multiplier grouped by (day_of_week, hour_of_day).
        Only includes slots with at least 2 samples.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT
                    CAST(strftime('%w', logged_at) AS INTEGER)  AS day_of_week_sun,
                    CAST(strftime('%H', logged_at) AS INTEGER)  AS hour_of_day,
                    AVG(session_multiplier)                      AS avg_multiplier,
                    COUNT(*)                                     AS sample_count
                FROM multiplier_log
                WHERE logged_at >= datetime('now', ? || ' days')
                GROUP BY day_of_week_sun, hour_of_day
                HAVING sample_count >= 2
                ORDER BY avg_multiplier DESC
                """,
                (f"-{days_back}",),
            ).fetchall()
        # Convert Sunday-based weekday (0=Sun) to Monday-based (0=Mon)
        result = []
        for r in rows:
            d = dict(r)
            d["day_of_week"] = (d.pop("day_of_week_sun") - 1) % 7
            result.append(d)
        return result

    def get_recent_sessions(self, days_back: int = 30) -> List[dict]:
        """Return session records from the last N days, newest first."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE start_time >= datetime('now', ? || ' days')
                ORDER BY start_time DESC
                """,
                (f"-{days_back}",),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Maintenance ───────────────────────────────────────────────────────────

    def export_csv(self, filepath: Path):
        """Write all session records to a CSV file."""
        sessions = self.get_recent_sessions(days_back=365)
        if not sessions:
            logger.warning("No sessions to export")
            return
        with open(filepath, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(sessions[0].keys()))
            writer.writeheader()
            writer.writerows(sessions)
        logger.info("Exported %d sessions to %s", len(sessions), filepath)

    def cleanup_old_data(self, retention_days: int = 90):
        """Delete records older than retention_days to keep the DB small."""
        cutoff = f"-{retention_days}"
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE created_at < datetime('now', ? || ' days')",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM hourly_snapshots WHERE snapshot_time < datetime('now', ? || ' days')",
                (cutoff,),
            )
