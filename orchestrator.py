#!/usr/bin/env python3
"""Dry-run scan → route → queue → report orchestration CLI."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from dataclasses import asdict
from pathlib import Path

from app_queue import ApplicationQueue
from audit_log import AuditLogger
import file_lock
import pipeline
import scanner


BASE = Path.home() / "Documents/job-agent"
VALID_SOURCE_HEALTH_STATUSES = {"healthy", "partial_error", "stale_or_unknown"}


class RunLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = None

    def __enter__(self) -> "RunLock":
        self._handle = self.path.open("a+", encoding="utf-8")
        if not file_lock.try_lock(self._handle.fileno()):
            self._handle.close()
            self._handle = None
            raise SystemExit("Another job-agent run is already active")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._handle is not None
        file_lock.unlock(self._handle.fileno())
        self._handle.close()
        self._handle = None


def build_scan(candidates: list[dict], profile: dict, *, known_urls=(), duplicate_checker=None, use_tracker: bool = False) -> dict:
    jobs = scanner.unique_jobs(candidates)
    known_urls = tuple(known_urls)
    results = [scanner.classify(job, profile, known_urls=known_urls,
                                duplicate_checker=duplicate_checker, use_tracker=use_tracker) for job in jobs]
    new = [job for job in results if job["relevant"] and not job["duplicate"]]
    manual = [job for job in new if job["manual_only"]]
    queue = [job for job in new if not job["manual_only"]]
    return {
        "scanned": len(results),
        "new": len(new),
        "manual_only": manual,
        "auto_apply_queue": queue,
        "all_results": results,
        "needs_verification": [job for job in results if job["eligibility_status"] == "needs_verification"],
    }


def enqueue_plan_jobs(queue_db: Path, routed: dict[str, list[dict]]) -> list[dict]:
    app_queue = ApplicationQueue(queue_db)
    for key in ("greenhouse", "workday"):
        for job in routed.get(key, []):
            app_queue.enqueue(
                company=job["company"],
                role=job["role"],
                url=job["url"],
                ats_platform=job["ats_platform"],
            )
    return [asdict(job) for job in app_queue.list_jobs()]


def _has_non_ok_source_runs(report: dict) -> bool:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return False
    for run in source_runs:
        if not isinstance(run, dict):
            continue
        if run.get("status") != "ok":
            return True
        if run.get("stale_result") or run.get("freshness_unknown"):
            return True
    return False


def _has_duplicate_source_run_identity(report: dict) -> bool:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return False
    seen: set[tuple[object, object]] = set()
    for run in source_runs:
        if not isinstance(run, dict):
            continue
        identity = (run.get("source"), run.get("token"))
        if identity in seen:
            return True
        seen.add(identity)
    return False


def _has_invalid_source_run_schema(report: dict) -> bool:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return False
    for run in source_runs:
        if not isinstance(run, dict):
            return True
        if not isinstance(run.get("source"), str) or not run["source"].strip():
            return True
        if not isinstance(run.get("token"), str) or not run["token"].strip():
            return True
        if run.get("status") != "ok":
            return True
        candidates = run.get("candidates")
        if not isinstance(candidates, int) or candidates < 0:
            return True
        if "stale_result" in run and not isinstance(run["stale_result"], bool):
            return True
        if "freshness_unknown" in run and not isinstance(run["freshness_unknown"], bool):
            return True
        if "warning" in run and not isinstance(run["warning"], str):
            return True
        if "latest_posting_at" in run:
            latest_posting_at = run["latest_posting_at"]
            if not isinstance(latest_posting_at, str) or not _is_valid_timestamp_string(latest_posting_at):
                return True
    return False


def _has_invalid_failures_schema(report: dict) -> bool:
    failures = report.get("failures", [])
    if not isinstance(failures, list):
        return True
    for failure in failures:
        if not isinstance(failure, dict):
            return True
        if not isinstance(failure.get("source"), str) or not failure["source"].strip():
            return True
        if not isinstance(failure.get("token"), str) or not failure["token"].strip():
            return True
        if not isinstance(failure.get("error"), str) or not failure["error"].strip():
            return True
    return False


def _parse_timestamp_string(value: str) -> datetime | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_valid_timestamp_string(value: str) -> bool:
    return _parse_timestamp_string(value) is not None


def _has_invalid_top_level_source_health_flags_schema(report: dict) -> bool:
    for field_name in ("stale_result", "freshness_unknown"):
        if field_name in report and not isinstance(report[field_name], bool):
            return True
    if "warning" in report and not isinstance(report["warning"], str):
        return True
    if "latest_posting_at" in report:
        latest_posting_at = report["latest_posting_at"]
        if not isinstance(latest_posting_at, str) or not _is_valid_timestamp_string(latest_posting_at):
            return True
    return False


def _has_mixed_timestamp_awareness(report: dict) -> bool:
    parsed_timestamps: list[datetime] = []
    top_level_timestamp = report.get("latest_posting_at")
    if isinstance(top_level_timestamp, str):
        parsed_timestamp = _parse_timestamp_string(top_level_timestamp)
        if parsed_timestamp is not None:
            parsed_timestamps.append(parsed_timestamp)

    source_runs = report.get("source_runs")
    if isinstance(source_runs, list):
        for run in source_runs:
            if not isinstance(run, dict):
                continue
            raw_timestamp = run.get("latest_posting_at")
            if not isinstance(raw_timestamp, str):
                continue
            parsed_timestamp = _parse_timestamp_string(raw_timestamp)
            if parsed_timestamp is not None:
                parsed_timestamps.append(parsed_timestamp)

    has_naive = any(timestamp.tzinfo is None or timestamp.tzinfo.utcoffset(timestamp) is None for timestamp in parsed_timestamps)
    has_aware = any(timestamp.tzinfo is not None and timestamp.tzinfo.utcoffset(timestamp) is not None for timestamp in parsed_timestamps)
    return has_naive and has_aware


def _expected_latest_posting_at(report: dict) -> str | None:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return None
    latest_timestamp: tuple[datetime, str] | None = None
    for run in source_runs:
        if not isinstance(run, dict):
            continue
        raw_timestamp = run.get("latest_posting_at")
        if not isinstance(raw_timestamp, str):
            continue
        parsed_timestamp = _parse_timestamp_string(raw_timestamp)
        if parsed_timestamp is None:
            continue
        if latest_timestamp is None or parsed_timestamp > latest_timestamp[0]:
            latest_timestamp = (parsed_timestamp, raw_timestamp)
    if latest_timestamp is None:
        return None
    return latest_timestamp[1]


def _has_missing_source_run_latest_posting_at(report: dict) -> bool:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return False
    for run in source_runs:
        if not isinstance(run, dict):
            continue
        if "latest_posting_at" not in run:
            return True
    return False


def _has_inconsistent_top_level_source_health_flags(report: dict) -> bool:
    source_runs = report.get("source_runs")
    if source_runs == [] and any(field_name in report for field_name in ("warning", "stale_result", "freshness_unknown")):
        return True
    if report.get("stale_result") or report.get("freshness_unknown"):
        return True
    if _has_missing_source_run_latest_posting_at(report):
        return True
    expected_latest_posting_at = _expected_latest_posting_at(report)
    if expected_latest_posting_at is not None and "latest_posting_at" not in report:
        return True
    if "latest_posting_at" in report:
        if expected_latest_posting_at is None:
            return True
        top_level_timestamp = _parse_timestamp_string(report["latest_posting_at"])
        expected_timestamp = _parse_timestamp_string(expected_latest_posting_at)
        if top_level_timestamp is None or expected_timestamp is None:
            return True
        return top_level_timestamp != expected_timestamp
    return False


def _expected_freshness_summary(report: dict) -> dict[str, int] | None:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return None
    stale_runs = sum(1 for run in source_runs if isinstance(run, dict) and run.get("stale_result"))
    freshness_unknown_runs = sum(1 for run in source_runs if isinstance(run, dict) and run.get("freshness_unknown"))
    error_runs = sum(1 for run in source_runs if isinstance(run, dict) and run.get("status") == "error")
    return {
        "total_runs": len(source_runs),
        "healthy_runs": len(source_runs) - stale_runs - freshness_unknown_runs - error_runs,
        "stale_runs": stale_runs,
        "freshness_unknown_runs": freshness_unknown_runs,
        "error_runs": error_runs,
    }


def _has_invalid_freshness_summary_schema(report: dict) -> bool:
    freshness_summary = report.get("freshness_summary")
    if not isinstance(freshness_summary, dict):
        return False
    for key in (
        "total_runs",
        "healthy_runs",
        "stale_runs",
        "freshness_unknown_runs",
        "error_runs",
    ):
        value = freshness_summary.get(key)
        if type(value) is not int or value < 0:
            return True
    return False


def _has_inconsistent_freshness_summary(report: dict) -> bool:
    freshness_summary = report.get("freshness_summary")
    if not isinstance(freshness_summary, dict):
        return False
    expected = _expected_freshness_summary(report)
    if expected is None:
        return False
    return freshness_summary != expected


def _expected_freshness_buckets(report: dict) -> dict[str, list[dict[str, str]]] | None:
    source_runs = report.get("source_runs")
    if not isinstance(source_runs, list):
        return None
    buckets = {
        "healthy": [],
        "stale": [],
        "freshness_unknown": [],
        "error": [],
    }
    for run in source_runs:
        if not isinstance(run, dict):
            continue
        entry = {"source": run.get("source"), "token": run.get("token")}
        if run.get("status") == "error":
            buckets["error"].append(entry)
        elif run.get("freshness_unknown"):
            buckets["freshness_unknown"].append(entry)
        elif run.get("stale_result"):
            buckets["stale"].append(entry)
        else:
            buckets["healthy"].append(entry)
    return buckets


def _has_invalid_freshness_buckets_schema(report: dict) -> bool:
    freshness_buckets = report.get("freshness_buckets")
    if not isinstance(freshness_buckets, dict):
        return False
    for bucket_name in ("healthy", "stale", "freshness_unknown", "error"):
        entries = freshness_buckets.get(bucket_name)
        if not isinstance(entries, list):
            return True
        for entry in entries:
            if not isinstance(entry, dict):
                return True
            if not isinstance(entry.get("source"), str) or not entry["source"].strip():
                return True
            if not isinstance(entry.get("token"), str) or not entry["token"].strip():
                return True
    return False


def _has_inconsistent_freshness_buckets(report: dict) -> bool:
    freshness_buckets = report.get("freshness_buckets")
    if not isinstance(freshness_buckets, dict):
        return False
    expected = _expected_freshness_buckets(report)
    if expected is None:
        return False
    return freshness_buckets != expected


def load_source_report(source_report_path: Path | None) -> dict | None:
    if source_report_path is None:
        return None
    try:
        report = json.loads(source_report_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit("Invalid source report: unreadable") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit("Invalid source report: invalid_json") from exc
    if not isinstance(report, dict):
        raise SystemExit("Invalid source report: invalid_schema")
    if "source_health_status" not in report:
        raise SystemExit("Invalid source report: missing_source_health_status")
    status = report["source_health_status"]
    if not isinstance(status, str) or status not in VALID_SOURCE_HEALTH_STATUSES:
        raise SystemExit("Invalid source report: invalid_source_health_status")
    if status == "healthy" and not isinstance(report.get("source_runs"), list):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and not isinstance(report.get("freshness_summary"), dict):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and not isinstance(report.get("freshness_buckets"), dict):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and _has_invalid_failures_schema(report):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and _has_invalid_source_run_schema(report):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and _has_invalid_freshness_summary_schema(report):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and _has_invalid_freshness_buckets_schema(report):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and _has_invalid_top_level_source_health_flags_schema(report):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and _has_mixed_timestamp_awareness(report):
        raise SystemExit("Invalid source report: invalid_schema")
    if status == "healthy" and (
        _has_inconsistent_top_level_source_health_flags(report)
        or report.get("failures")
        or _has_non_ok_source_runs(report)
        or _has_duplicate_source_run_identity(report)
        or _has_inconsistent_freshness_summary(report)
        or _has_inconsistent_freshness_buckets(report)
    ):
        raise SystemExit("Invalid source report: inconsistent_source_health")
    if status != "healthy":
        raise SystemExit(f"Source health check failed: {status}")
    return report


def run(
    candidates_path: Path,
    profile_path: Path,
    output_path: Path,
    queue_db: Path,
    audit_log_path: Path,
    source_report_path: Path | None = None,
) -> dict:
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    errors = pipeline.validate_profile(profile)
    if errors:
        raise SystemExit("Invalid profile: " + ", ".join(errors))

    source_report = load_source_report(source_report_path)

    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    known_urls = scanner.known_urls_from_queue(queue_db)
    scan = build_scan(candidates, profile, known_urls=known_urls)
    plan = {
        "mode": "plan_only",
        "submission_enabled": False,
        "routes": pipeline.route_candidates(scan),
    }
    plan["counts"] = {key: len(value) for key, value in plan["routes"].items()}

    queued_jobs = enqueue_plan_jobs(queue_db, plan["routes"])
    payload = {
        "mode": "dry_run",
        "scan": scan,
        "plan": plan,
        "queue": {"count": len(queued_jobs), "jobs": queued_jobs},
    }
    if source_report is not None:
        payload["source_report"] = source_report
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    logger = AuditLogger(audit_log_path)
    logger.log(
        "dry_run_completed",
        {
            "profile": profile,
            "counts": payload["plan"]["counts"],
            "queue_count": payload["queue"]["count"],
            "output": str(output_path),
        },
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates", help="Verified candidates JSON array")
    parser.add_argument("--profile", default=str(BASE / "profile.json"))
    parser.add_argument("--output", default=str(BASE / "orchestrator-report.json"))
    parser.add_argument("--queue-db", default=str(BASE / "runtime/app_queue.sqlite3"))
    parser.add_argument("--audit-log", default=str(BASE / "runtime/audit.jsonl"))
    parser.add_argument("--lock-path", default=str(BASE / "runtime/orchestrator.lock"))
    parser.add_argument("--source-report", help="Optional sources.py report JSON path")
    args = parser.parse_args(argv)

    with RunLock(Path(args.lock_path)):
        payload = run(
            Path(args.candidates),
            Path(args.profile),
            Path(args.output),
            Path(args.queue_db),
            Path(args.audit_log),
            Path(args.source_report) if args.source_report else None,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
