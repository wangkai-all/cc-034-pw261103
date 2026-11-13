"""SQLite persistence layer (stdlib sqlite3, no ORM)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_dir TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    config_json TEXT NOT NULL,
    progress_json TEXT,
    scan_error TEXT,
    last_scan_at TEXT
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    package_key TEXT NOT NULL,
    package_code TEXT,
    relative_path TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'detected',
    failure_code TEXT,
    reason TEXT,
    source_fingerprint TEXT,
    manifest_sha256 TEXT,
    zip_name TEXT,
    zip_sha256 TEXT,
    zip_bytes INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER NOT NULL DEFAULT 0,
    total_uncompressed INTEGER NOT NULL DEFAULT 0,
    batch_id INTEGER,
    progress_json TEXT,
    objects_json TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, package_key)
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    package_code TEXT NOT NULL,
    zip_sha256 TEXT NOT NULL,
    zip_name TEXT,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source_package_id INTEGER,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    publish_path TEXT,
    publish_reason TEXT,
    UNIQUE (task_id, package_code, zip_sha256)
);

CREATE TABLE IF NOT EXISTS batch_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    full_path TEXT,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    result TEXT NOT NULL,
    reason TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (batch_id, rel_path)
);

CREATE TABLE IF NOT EXISTS manifest_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL,
    expected_bytes INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    actual_bytes INTEGER,
    actual_sha256 TEXT,
    result TEXT NOT NULL,
    reason TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (package_id, rel_path)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    package_id INTEGER,
    batch_id INTEGER,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_packages_task_state ON packages(task_id, state);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_manifest_package ON manifest_entries(package_id, sort_order);
CREATE INDEX IF NOT EXISTS idx_batches_code ON batches(task_id, package_code);
CREATE INDEX IF NOT EXISTS idx_batch_files ON batch_files(batch_id, sort_order);
"""


class Database:
    """One connection guarded by a lock; the workload is small and local."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(p) for p in params])
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def add_event(
        self,
        task_id: int,
        level: str,
        code: str,
        message: str,
        *,
        package_id: int | None = None,
        batch_id: int | None = None,
        detail: dict[str, Any] | None = None,
        created_at: str,
    ) -> None:
        self.execute(
            "INSERT INTO events (task_id, package_id, batch_id, level, code, message,"
            " detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                package_id,
                batch_id,
                level,
                code,
                message,
                json.dumps(detail, ensure_ascii=False) if detail else None,
                created_at,
            ),
        )
