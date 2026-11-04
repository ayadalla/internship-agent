#!/usr/bin/env python3
"""Transactional SQLite execution journal; imports legacy JSONL without destroying it.

New paths contain SQLite even when their suffix is .jsonl. Existing nonempty
JSONL is imported once into <path>.sqlite3; malformed lines are quarantined by
line number and never prevent another job from being read. Stop legacy writers
before migrating. ``db_path`` identifies the actual SQLite database.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ExecutionJournal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = b""
        if self.path.exists():
            with self.path.open("rb") as handle:
                header = handle.read(16)
        legacy = bool(header and header != b"SQLite format 3\x00")
        self.db_path = Path(str(self.path) + ".sqlite3") if legacy else self.path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS execution_entries (
                id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, attempt_count INTEGER NOT NULL,
                step TEXT NOT NULL, entry_json TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS execution_job ON execution_entries(job_id, attempt_count, id)")
            conn.execute("CREATE TABLE IF NOT EXISTS journal_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS journal_quarantine (line_number INTEGER PRIMARY KEY)")
            imported = conn.execute("SELECT 1 FROM journal_metadata WHERE key = 'legacy_imported'").fetchone()
            if legacy and not imported:
                for number, line in enumerate(self.path.read_text(errors="replace", encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if (not isinstance(entry, dict) or not isinstance(entry.get("job_id"), int)
                                or not isinstance(entry.get("attempt_count"), int)
                                or not isinstance(entry.get("step"), str)
                                or not isinstance(entry.get("payload"), dict)):
                            raise ValueError("invalid journal entry")
                    except (ValueError, TypeError):
                        conn.execute("INSERT INTO journal_quarantine VALUES (?)", (number,))
                        continue
                    self._insert(conn, entry)
                conn.execute("INSERT INTO journal_metadata VALUES ('legacy_imported', '1')")

    @staticmethod
    def _insert(conn: sqlite3.Connection, entry: dict[str, Any]) -> None:
        conn.execute("""INSERT INTO execution_entries(job_id, attempt_count, step, entry_json)
                     VALUES (?, ?, ?, ?)""",
                     (entry["job_id"], entry["attempt_count"], entry["step"], json.dumps(entry, sort_keys=True)))

    def append(self, *, job_id: int, attempt_count: int, step: str, payload: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job_id": job_id,
            "attempt_count": attempt_count,
            "step": step,
            "payload": payload,
        }
        with sqlite3.connect(self.db_path) as conn:
            self._insert(conn, entry)
        return entry

    def read_all(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT entry_json FROM execution_entries ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def entries_for(self, *, job_id: int, attempt_count: int) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""SELECT entry_json FROM execution_entries
                                WHERE job_id = ? AND attempt_count = ? ORDER BY id""",
                                (job_id, attempt_count)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def latest_checkpoint(self, *, job_id: int, attempt_count: int, after_attempt: int = 0) -> dict[str, Any] | None:
        """Stable per-job progress through this attempt, not the last lease event.

        A finished/blocked attempt invalidates older preparation; new lease_claimed
        audit entries do not hide a plan persisted immediately before a crash.
        Generation ordering prevents late events from older owners replacing newer progress.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""SELECT entry_json FROM execution_entries
                WHERE job_id = ? AND attempt_count <= ? AND attempt_count > ? AND step != 'lease_claimed'
                ORDER BY attempt_count DESC, id DESC""", (job_id, attempt_count, after_attempt)).fetchall()
        for row in rows:
            entry = json.loads(row[0])
            if entry["step"] in {"lease_finished", "prepare_blocked"}:
                return None
            if entry["step"] == "prepared_plan_written":
                return entry
        return None

    def latest_step(self, *, job_id: int, attempt_count: int) -> dict[str, Any] | None:
        entries = self.entries_for(job_id=job_id, attempt_count=attempt_count)
        return entries[-1] if entries else None
