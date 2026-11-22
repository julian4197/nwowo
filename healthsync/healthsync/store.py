"""SQLite storage: raw API responses plus one row of flat metrics per day."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (day, source, kind)
);
CREATE TABLE IF NOT EXISTS daily (
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    metrics TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (day, source)
);
"""


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_raw(con: sqlite3.Connection, day: str, source: str, raw: dict[str, Any]) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO raw VALUES (?, ?, ?, ?, ?)",
        [(day, source, kind, json.dumps(v), _now()) for kind, v in raw.items() if v is not None],
    )
    con.commit()


def save_daily(con: sqlite3.Connection, day: str, source: str, metrics: dict[str, Any]) -> None:
    con.execute(
        "INSERT OR REPLACE INTO daily VALUES (?, ?, ?, ?)",
        (day, source, json.dumps(metrics), _now()),
    )
    con.commit()


def load_daily(con: sqlite3.Connection, since: str) -> dict[str, dict[str, Any]]:
    """Merge all sources per day. Later sources fill gaps, they never overwrite."""
    out: dict[str, dict[str, Any]] = {}
    rows = con.execute(
        "SELECT day, source, metrics FROM daily WHERE day >= ? ORDER BY day, source", (since,)
    )
    for day, _source, metrics in rows:
        merged = out.setdefault(day, {})
        for k, v in json.loads(metrics).items():
            if merged.get(k) is None:
                merged[k] = v
    return out


def load_source_day(con: sqlite3.Connection, day: str, source: str) -> dict[str, Any]:
    row = con.execute("SELECT metrics FROM daily WHERE day = ? AND source = ?", (day, source)).fetchone()
    return json.loads(row[0]) if row else {}
