"""Append-only SME decision log. Decisions feed back into ranking as evidence."""
from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from pathlib import Path

from .knowledge import Evidence

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    target_table TEXT NOT NULL, target_column TEXT NOT NULL DEFAULT '',
    source_table TEXT NOT NULL, source_column TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'needs_research')),
    confidence REAL, reviewer TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS decisions_pair ON decisions(target_table, target_column, source_table, source_column, id);
"""

LATEST = """
SELECT d.* FROM decisions d
WHERE d.id = (SELECT max(e.id) FROM decisions e
              WHERE e.target_table = d.target_table AND e.target_column = d.target_column
                AND e.source_table = d.source_table AND e.source_column = d.source_column)
"""


class FeedbackStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript(SCHEMA)
        self._cache = None

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def record(self, *, target_table: str, target_column: str | None, source_table: str,
               source_column: str | None, decision: str, reviewer: str, note: str = "",
               confidence: float | None = None) -> int:
        if not reviewer.strip():
            raise ValueError("Reviewer is required.")
        if decision not in {"approved", "rejected", "needs_research"}:
            raise ValueError("Decision must be approved, rejected or needs_research.")
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "INSERT INTO decisions(target_table, target_column, source_table, source_column, "
                "decision, confidence, reviewer, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (target_table, target_column or "", source_table, source_column or "", decision,
                 confidence, reviewer.strip(), note.strip()))
            self._cache = None
            return cursor.lastrowid

    def latest(self) -> list[dict]:
        if self._cache is None:
            with closing(self._connect()) as db:
                self._cache = [dict(r) for r in db.execute(LATEST + " ORDER BY d.id")]
        return self._cache

    def history(self, limit: int = 200) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))]

    def evidence(self, target_table: str, target_column: str | None, source_table: str,
                 source_column: str | None) -> list[Evidence]:
        found = []
        for row in self.latest():
            if (row["target_table"], row["target_column"], row["source_table"], row["source_column"]) != \
                    (target_table, target_column or "", source_table, source_column or ""):
                continue
            if row["decision"] == "approved":
                found.append(Evidence("feedback", 1.0, f"Approved by {row['reviewer']}.",
                                      f"review #{row['id']} ({row['created_at'][:10]})", row["note"]))
            elif row["decision"] == "rejected":
                found.append(Evidence("feedback", -1.0, f"Rejected by {row['reviewer']}.",
                                      f"review #{row['id']} ({row['created_at'][:10]})", row["note"]))
        return found

    def export_s2t(self, path: str | Path) -> int:
        """Write approved decisions as an S2T mapping sheet that can be re-ingested as knowledge."""
        rows = [r for r in self.latest() if r["decision"] == "approved"]
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["target_table", "target_column", "source_table", "source_column",
                             "confidence", "reviewer", "notes", "approved_at"])
            for r in rows:
                writer.writerow([r["target_table"], r["target_column"], r["source_table"],
                                 r["source_column"], r["confidence"], r["reviewer"], r["note"],
                                 r["created_at"]])
        return len(rows)
