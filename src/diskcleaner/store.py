"""SQLite storage for scans and labels.

Lives in the OS user-data folder (or $DISKCLEANER_HOME if set), never in
the repo or next to the scanned files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_dir

from diskcleaner.scanner import Candidate, ScanResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    root         TEXT NOT NULL,
    scanned_at   REAL NOT NULL,
    dirs_scanned INTEGER,
    files_seen   INTEGER,
    bytes_seen   INTEGER,
    errors       INTEGER
);
CREATE TABLE IF NOT EXISTS candidates (
    scan_id      INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    data         TEXT NOT NULL,
    category     TEXT NOT NULL,
    prior        REAL NOT NULL,
    duplicate_of TEXT,
    rejected     TEXT,
    PRIMARY KEY (scan_id, path)
);
CREATE TABLE IF NOT EXISTS labels (
    path         TEXT PRIMARY KEY,
    label        TEXT NOT NULL CHECK (label IN ('keep', 'delete', 'skip')),
    data         TEXT NOT NULL,
    category     TEXT NOT NULL,
    is_duplicate INTEGER NOT NULL,
    labeled_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    root         TEXT NOT NULL,
    taken_at     REAL NOT NULL,
    depth        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_sizes (
    snapshot_id  INTEGER NOT NULL REFERENCES usage_snapshots(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, path)
);
"""


def data_dir() -> Path:
    override = os.environ.get("DISKCLEANER_HOME")
    return Path(override) if override else Path(user_data_dir("diskcleaner", appauthor=False))


@dataclass
class StoredCandidate:
    candidate: Candidate
    category: str
    prior: float
    duplicate_of: str | None
    rejected: str | None


@dataclass
class Label:
    candidate: Candidate
    label: str
    category: str
    is_duplicate: bool
    labeled_at: float


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "diskcleaner.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def save_scan(self, result: ScanResult, rows: list[StoredCandidate]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO scans (root, scanned_at, dirs_scanned, files_seen, bytes_seen, errors)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    result.root,
                    time.time(),
                    result.dirs_scanned,
                    result.files_seen,
                    result.bytes_seen,
                    result.errors,
                ),
            )
            scan_id = cur.lastrowid
            self.conn.executemany(
                "INSERT OR REPLACE INTO candidates VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        scan_id,
                        r.candidate.path,
                        json.dumps(r.candidate.to_dict()),
                        r.category,
                        r.prior,
                        r.duplicate_of,
                        r.rejected,
                    )
                    for r in rows
                ],
            )
        return scan_id

    def latest_scan(self) -> tuple[int, str, float] | None:
        row = self.conn.execute(
            "SELECT id, root, scanned_at FROM scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return tuple(row) if row else None

    def candidates(self, scan_id: int, *, include_rejected: bool = False) -> list[StoredCandidate]:
        sql = (
            "SELECT data, category, prior, duplicate_of, rejected FROM candidates WHERE scan_id = ?"
        )
        if not include_rejected:
            sql += " AND rejected IS NULL"
        out = []
        for data, category, prior, dup, rejected in self.conn.execute(sql, (scan_id,)):
            c = Candidate(**json.loads(data))
            out.append(StoredCandidate(c, category, prior, dup, rejected))
        return out

    def set_label(self, sc: StoredCandidate, label: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?)",
                (
                    sc.candidate.path,
                    label,
                    json.dumps(sc.candidate.to_dict()),
                    sc.category,
                    int(sc.duplicate_of is not None),
                    time.time(),
                ),
            )

    def clear_label(self, path: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM labels WHERE path = ?", (path,))

    def labels(self) -> list[Label]:
        rows = self.conn.execute(
            "SELECT data, label, category, is_duplicate, labeled_at FROM labels"
        ).fetchall()
        return [
            Label(Candidate(**json.loads(d)), lab, cat, bool(dup), ts)
            for d, lab, cat, dup, ts in rows
        ]

    def label_map(self) -> dict[str, str]:
        return dict(self.conn.execute("SELECT path, label FROM labels"))

    def label_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT label, COUNT(*) FROM labels GROUP BY label"))

    def save_usage(self, root: str, rows: list[tuple[str, int]], depth: int) -> None:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO usage_snapshots (root, taken_at, depth) VALUES (?, ?, ?)",
                (root, time.time(), depth),
            )
            sid = cur.lastrowid
            self.conn.executemany(
                "INSERT OR REPLACE INTO usage_sizes VALUES (?, ?, ?)",
                [(sid, path, size) for path, size in rows],
            )
            # only the latest is compared against, keep a few in case that changes
            self.conn.execute(
                "DELETE FROM usage_snapshots WHERE root = ? AND id NOT IN "
                "(SELECT id FROM usage_snapshots WHERE root = ? ORDER BY id DESC LIMIT 5)",
                (root, root),
            )

    def last_usage(self, root: str) -> tuple[float, int, dict[str, int]] | None:
        """(taken_at, depth, {path: size}) from the most recent snapshot of root."""
        row = self.conn.execute(
            "SELECT id, taken_at, depth FROM usage_snapshots WHERE root = ? "
            "ORDER BY id DESC LIMIT 1",
            (root,),
        ).fetchone()
        if row is None:
            return None
        sid, taken_at, depth = row
        sizes = dict(
            self.conn.execute("SELECT path, size FROM usage_sizes WHERE snapshot_id = ?", (sid,))
        )
        return taken_at, depth, sizes
