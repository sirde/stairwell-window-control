"""SQLite persistence: window event history + weather snapshots.

A connection is opened per operation. Volume is tiny (a handful of windows,
one weather poll every few minutes), so there's no pooling to worry about,
and opening per-call sidesteps the cross-thread sharing issues between the
Flask request threads and the weather-poller thread.
"""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

log = logging.getLogger(__name__)


def iso_utc_now() -> str:
    """Current time as a second-resolution ISO 8601 UTC string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_FILE, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def _db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         TEXT    NOT NULL,            -- ISO 8601 UTC
                building   TEXT,
                window     TEXT,
                action     TEXT    NOT NULL,            -- open | close | reset
                source     TEXT    NOT NULL,            -- manual | advisory | auto | system
                actor      TEXT,                        -- username, trigger token, or 'startup'
                reason     TEXT,
                success    INTEGER,                     -- 1 | 0 | NULL (n/a)
                conditions TEXT                         -- JSON weather snapshot at decision time
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weather_snapshots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    NOT NULL,          -- ISO 8601 UTC
                temp_c        REAL,
                wind_gust_kmh REAL,
                rain_prob     INTEGER,                  -- max % over lookahead
                advise_close  INTEGER,                  -- 1 | 0  (wind AND rain)
                caution       INTEGER,                  -- 1 | 0  (high-risk day flag)
                raw           TEXT                      -- JSON summary
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_weather_ts ON weather_snapshots(ts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS radar_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,            -- ISO 8601 UTC
                rain_near   INTEGER,                    -- 1 | 0
                nearest_km  REAL,                       -- distance to nearest echo
                approaching INTEGER,                    -- 1 | 0
                eta_min     INTEGER,                    -- minutes until rain (nowcast)
                raw         TEXT                        -- JSON summary
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_radar_ts ON radar_snapshots(ts)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # Idempotent migrations for DBs created by an earlier schema.
        _add_column(conn, "weather_snapshots", "caution", "INTEGER")
    log.info("SQLite initialised at %s", config.DB_FILE)


def _add_column(conn, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        log.info("Added column %s.%s", table, column)


def record_event(action: str, *, building=None, window=None, source="manual",
                 actor=None, reason=None, success=None, conditions=None) -> None:
    with _db() as conn:
        conn.execute(
            """INSERT INTO events
               (ts, building, window, action, source, actor, reason, success, conditions)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (iso_utc_now(), building, window, action, source, actor, reason,
             None if success is None else int(success),
             json.dumps(conditions) if conditions is not None else None),
        )


def recent_events(limit: int = 300) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def record_weather(*, temp_c, wind_gust_kmh, rain_prob, advise_close, caution, raw) -> None:
    with _db() as conn:
        conn.execute(
            """INSERT INTO weather_snapshots
               (ts, temp_c, wind_gust_kmh, rain_prob, advise_close, caution, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (iso_utc_now(), temp_c, wind_gust_kmh, rain_prob,
             int(bool(advise_close)), int(bool(caution)), json.dumps(raw)),
        )


def recent_weather(limit: int = 300) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM weather_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def record_radar(*, rain_near, nearest_km, approaching, eta_min, raw) -> None:
    with _db() as conn:
        conn.execute(
            """INSERT INTO radar_snapshots
               (ts, rain_near, nearest_km, approaching, eta_min, raw)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (iso_utc_now(), int(bool(rain_near)), nearest_km,
             int(bool(approaching)), eta_min, json.dumps(raw)),
        )


def recent_radar(limit: int = 300) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM radar_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# --- Settings (key/value) ----------------------------------------------------
# Automation toggles gate live actuation (see weather.py): enabled = the poller
# drives the relay for real on a matching trigger; disabled = the trigger is
# still logged (advisory / simulated) so the strategy can keep being calibrated.
AUTO_OPEN_TEMP_MIN = 15
AUTO_OPEN_TEMP_MAX = 30
AUTO_OPEN_TEMP_DEFAULT = 22


def get_automation() -> dict:
    """Current automation settings (with sane defaults)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('auto_open_enabled', 'auto_open_temp_c', 'auto_close_enabled')"
        ).fetchall()
    vals = {r["key"]: r["value"] for r in rows}
    try:
        temp_c = int(vals.get("auto_open_temp_c", AUTO_OPEN_TEMP_DEFAULT))
    except (TypeError, ValueError):
        temp_c = AUTO_OPEN_TEMP_DEFAULT
    temp_c = max(AUTO_OPEN_TEMP_MIN, min(AUTO_OPEN_TEMP_MAX, temp_c))
    return {
        "open_enabled": vals.get("auto_open_enabled") == "1",
        "open_temp_c": temp_c,
        "close_enabled": vals.get("auto_close_enabled") == "1",
    }


def set_automation(*, open_enabled=None, open_temp_c=None, close_enabled=None) -> dict:
    """Persist any provided automation fields; returns the full resulting state.

    Only the keyword arguments that are not None are written, so the UI can
    toggle one switch at a time. The open temperature is clamped to range.
    """
    updates = []
    if open_enabled is not None:
        updates.append(("auto_open_enabled", "1" if open_enabled else "0"))
    if close_enabled is not None:
        updates.append(("auto_close_enabled", "1" if close_enabled else "0"))
    if open_temp_c is not None:
        t = max(AUTO_OPEN_TEMP_MIN, min(AUTO_OPEN_TEMP_MAX, int(open_temp_c)))
        updates.append(("auto_open_temp_c", str(t)))
    if updates:
        with _db() as conn:
            conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                updates,
            )
    return get_automation()
