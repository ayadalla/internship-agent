#!/usr/bin/env python3
"""Lease-aware preparation worker with durable recovery journal."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import platform_io
import prepare_job
from app_queue import ApplicationQueue, LeaseLostError, QueueJob
from execution_journal import ExecutionJournal
from resume_preflight import preflight_profile_resume


class ATSCircuitBreaker:
    """Persistent per-ATS cooldowns that prevent one failed ATS from stalling work."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS ats_circuits (
                    platform TEXT PRIMARY KEY,
                    open_until TEXT NOT NULL,
                    failure_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(ats_circuits)")}
            if "last_failure_at" not in columns:
                conn.execute("ALTER TABLE ats_circuits ADD COLUMN last_failure_at TEXT")
            if "failure_count" not in columns:
                conn.execute(
                    "ALTER TABLE ats_circuits ADD COLUMN failure_count INTEGER NOT NULL DEFAULT 0"
                )

    def record_failure(self, *, platform: str, now: str, cooldown_seconds: int) -> int:
        opened_at = datetime.fromisoformat(now)
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=UTC)
        open_until = (opened_at.timestamp() + min(3600, max(1, cooldown_seconds)))
        until = datetime.fromtimestamp(open_until, tz=opened_at.tzinfo).isoformat()
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            normalized_platform = platform.casefold()
            row = conn.execute(
                "SELECT failure_count, open_until FROM ats_circuits WHERE platform = ?", (normalized_platform,)
            ).fetchone()
            failure_count = (row[0] if row and datetime.fromisoformat(row[1]) > opened_at else 0) + 1
            conn.execute(
                """INSERT OR REPLACE INTO ats_circuits (platform, open_until, failure_count, last_failure_at)
                   VALUES (?, ?, ?, ?)""",
                (normalized_platform, until, failure_count, opened_at.isoformat()),
            )
        return failure_count

    def record_success(self, *, platform: str, now: str) -> None:
        """Clear only failures no newer than this success observation."""
        with sqlite3.connect(self.path) as conn:
            conn.execute("""DELETE FROM ats_circuits WHERE platform = ?
                         AND (last_failure_at IS NULL OR julianday(last_failure_at) <= julianday(?))""",
                         (platform.casefold(), now))

    def open_platforms(self, *, now: str) -> tuple[str, ...]:
        current = datetime.fromisoformat(now)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT platform, open_until FROM ats_circuits").fetchall()
        return tuple(
            platform for platform, open_until in rows
            if datetime.fromisoformat(open_until) > current
        )


def _plan_path(*, plan_dir: Path, leased_job: QueueJob) -> Path:
    return Path(plan_dir) / f"job-{leased_job.id}-attempt-{leased_job.attempt_count}.json"


def _write_plan(path: Path, payload: dict[str, Any]) -> None:
    """Durably publish complete bytes before advertising the checkpoint in SQLite."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        platform_io.fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _closed_posting_error(html_text: str) -> str | None:
    for phrase in (
        "This job is no longer available.",
        "This position has been filled.",
        "This job is no longer accepting applications.",
    ):
        if phrase.casefold() in html_text.casefold():
            return f"Posting closed: {phrase}"
    return None


def _is_retryable_prepare_error(error: ValueError) -> bool:
    return not str(error).startswith((
        "Unsupported ATS for URL:",
        "Corrupted fixture:",
        "Posting closed:",
    ))


def _preparation_outcome(payload: dict[str, Any]) -> str:
    gates = []
    for key in ("manual_gate", "manual_gates", "human_gate", "human_gates"):
        value = payload.get(key)
        if value:
            gates.extend(value if isinstance(value, list) else [value])
    kinds = [str(gate.get("type", "") if isinstance(gate, dict) else gate).casefold() for gate in gates]
    if any(kind not in {"approval", "pending_approval", "missing_fact", "question", "unknown_fact"} for kind in kinds):
        return "blocked_security"
    if payload.get("approval_required") or any("approval" in kind for kind in kinds):
        return "blocked_approval"
    if kinds:
        return "blocked_fact"
    if "safe_to_prepare" in payload:
        return "prepared" if payload["safe_to_prepare"] is True else "blocked_fact"
    # Legacy Lever/Oracle inspectors predate safe_to_prepare. Require their
    # application inventory contract, rather than treating arbitrary {} as safe.
    if (payload.get("page_type") == "application" and payload.get("fields")
            and payload.get("uploaded_resume_verified") is not False
            and ("manual_gate" in payload or (payload.get("platform") == "oracle"
                 and payload.get("issues") == [] and payload.get("country_valid") is True))):
        return "prepared"
    return "blocked_fact"


def resume_or_prepare_leased_job(
    *,
    queue: ApplicationQueue,
    leased_job: QueueJob,
    journal: ExecutionJournal,
    html_text: str | Callable[[], str],
    expected_resume_basename: str | None,
    now: str,
    plan_dir: Path,
    resume_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    leased_job = queue.validate_lease(leased_job.id, lease_token=leased_job.lease_token, now=now)

    plan_dir = Path(plan_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    latest_entry = journal.latest_checkpoint(job_id=leased_job.id, attempt_count=leased_job.attempt_count,
                                             after_attempt=leased_job.checkpoint_after_attempt)
    recovered = False

    if latest_entry and latest_entry.get("step") == "prepared_plan_written":
        plan_path = Path(latest_entry.get("payload", {}).get("plan_path", ""))
        if not plan_path.is_file():
            raise ValueError(f"Invalid recovered plan artifact: {plan_path}")
        try:
            recovered_payload = json.loads(plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid recovered plan artifact: {plan_path}") from exc
        if not isinstance(recovered_payload, dict):
            raise ValueError(f"Invalid recovered plan artifact: {plan_path}")
        payload = recovered_payload
        recovered = True
    else:
        html_text = html_text() if callable(html_text) else html_text
        closed_error = _closed_posting_error(html_text)
        if closed_error:
            raise ValueError(closed_error)
        plan_path = _plan_path(plan_dir=plan_dir, leased_job=leased_job)
        payload = prepare_job.prepare_saved_html(
            html_text=html_text,
            page_url=leased_job.url,
            expected_resume_basename=expected_resume_basename,
        )
        if resume_evidence:
            payload["resume_preflight"] = resume_evidence
            payload["resume_verified"] = True
        queue.validate_lease(leased_job.id, lease_token=leased_job.lease_token, now=now)
        _write_plan(plan_path, payload)
        journal.append(
            job_id=leased_job.id,
            attempt_count=leased_job.attempt_count,
            step="prepared_plan_written",
            payload={
                "plan_path": str(plan_path),
                "platform": payload.get("platform"),
                "page_type": payload.get("page_type"),
            },
        )

    outcome = _preparation_outcome(payload)
    finished = queue.finish_lease(leased_job.id, lease_token=leased_job.lease_token, outcome=outcome, now=now)
    journal.append(
        job_id=leased_job.id,
        attempt_count=leased_job.attempt_count,
        step="lease_finished",
        payload={"state": finished.state, "plan_path": str(plan_path)},
    )
    return {
        "recovered": recovered,
        "plan_path": str(plan_path),
        "queue_job": asdict(finished),
    }


def prepare_next_job(
    *,
    queue: ApplicationQueue,
    journal: ExecutionJournal,
    html_loader,
    expected_resume_basename: str | None,
    now: str,
    lease_seconds: int,
    plan_dir: Path,
    circuit_breaker: ATSCircuitBreaker | None = None,
) -> dict[str, Any] | None:
    excluded_platforms = () if circuit_breaker is None else circuit_breaker.open_platforms(now=now)
    leased_job = queue.lease_next(
        now=now,
        lease_seconds=lease_seconds,
        excluded_platforms=excluded_platforms,
    )
    if leased_job is None:
        return None

    journal.append(
        job_id=leased_job.id,
        attempt_count=leased_job.attempt_count,
        step="lease_claimed",
        payload={
            "state": leased_job.state,
            "lease_expires_at": leased_job.lease_expires_at,
        },
    )
    return resume_or_prepare_leased_job(
        queue=queue,
        leased_job=leased_job,
        journal=journal,
        html_text=lambda: html_loader(leased_job),
        expected_resume_basename=expected_resume_basename,
        now=now,
        plan_dir=plan_dir,
    )



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-db", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--html-path", required=True)
    parser.add_argument("--now", required=True)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--plan-dir", required=True)
    parser.add_argument("--expected-resume-basename")
    parser.add_argument("--profile")
    parser.add_argument("--circuit-db")
    parser.add_argument("--circuit-cooldown-seconds", type=int, default=300)
    parser.add_argument("--ats-retry-budget", type=int, default=3)
    args = parser.parse_args(argv)

    try:
        resume_evidence = preflight_profile_resume(args.profile) if args.profile else None
    except ValueError as exc:
        print(json.dumps({
            "status": "prepare_blocked",
            "error": str(exc),
            "retryable": False,
            "submission_enabled": False,
        }))
        return 2
    expected_resume_basename = (
        resume_evidence["basename"] if resume_evidence else args.expected_resume_basename
    )

    html_path = Path(args.html_path)
    queue = ApplicationQueue(Path(args.queue_db), retry_budget=args.ats_retry_budget)
    journal = ExecutionJournal(Path(args.journal))
    circuit_breaker = (
        ATSCircuitBreaker(Path(args.circuit_db)) if args.circuit_db else None
    )
    excluded_platforms = () if circuit_breaker is None else circuit_breaker.open_platforms(now=args.now)
    leased_job = queue.lease_next(
        now=args.now,
        lease_seconds=args.lease_seconds,
        excluded_platforms=excluded_platforms,
    )
    if leased_job is None:
        print(json.dumps({"status": "no_job_available"}))
        return 0

    journal.append(
        job_id=leased_job.id,
        attempt_count=leased_job.attempt_count,
        step="lease_claimed",
        payload={
            "state": leased_job.state,
            "lease_expires_at": leased_job.lease_expires_at,
        },
    )

    try:
        result = resume_or_prepare_leased_job(
            queue=queue,
            leased_job=leased_job,
            journal=journal,
            html_text=html_path.read_text,
            expected_resume_basename=expected_resume_basename,
            now=args.now,
            plan_dir=Path(args.plan_dir),
            resume_evidence=resume_evidence,
        )
    except LeaseLostError as exc:
        print(json.dumps({"status": "lease_lost", "job_id": leased_job.id,
                          "error": str(exc), "submission_enabled": False}))
        return 2
    except ValueError as exc:
        retryable = _is_retryable_prepare_error(exc)
        retry_budget_exhausted = retryable and leased_job.attempt_count >= max(1, args.ats_retry_budget)
        if retryable and circuit_breaker is not None:
            circuit_breaker.record_failure(
                platform=leased_job.ats_platform,
                now=args.now,
                cooldown_seconds=args.circuit_cooldown_seconds,
            )
        if retry_budget_exhausted:
            retryable = False
        journal.append(
            job_id=leased_job.id,
            attempt_count=leased_job.attempt_count,
            step="prepare_blocked",
            payload={"error": str(exc), "retryable": retryable},
        )
        finished = queue.finish_lease(
            leased_job.id,
            lease_token=leased_job.lease_token,
            outcome="retry" if retryable else "failed",
            now=args.now,
            retry_seconds=0,
            error=str(exc),
        )
        journal.append(
            job_id=leased_job.id,
            attempt_count=leased_job.attempt_count,
            step="lease_finished",
            payload={"state": finished.state, "error": str(exc)},
        )
        response = {
            "status": "posting_closed" if str(exc).startswith("Posting closed:") else "prepare_blocked",
            "error": str(exc),
            "job_id": leased_job.id,
            "retryable": retryable,
            "submission_enabled": False,
        }
        if retry_budget_exhausted:
            response["retry_budget_exhausted"] = True
        print(json.dumps(response))
        return 2

    state = result["queue_job"]["state"]
    print(json.dumps({"status": state, "result": result}))
    return 0 if state == "prepared" else 2


if __name__ == "__main__":
    raise SystemExit(main())
