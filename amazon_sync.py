#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Callable

import app_queue
import tracker

BASE = Path.home() / "Documents/job-agent"
DEFAULT_QUEUE_DB = BASE / "runtime/app_queue.sqlite3"
DEFAULT_OUTPUT = BASE / "runtime/amazon-sync-report.json"
DEFAULT_BROAD_SCRIPT = Path.home() / ".hermes/scripts/amazon_2027_monitor.py"
DEFAULT_SDE_SCRIPT = Path.home() / ".hermes/scripts/amazon_2027_sde_monitor.py"


Job = dict[str, str]
Plan = dict[str, list[dict[str, str]]]


def load_monitor_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load monitor module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_monitor_jobs(script_path: Path, source_monitor: str) -> list[Job]:
    module = load_monitor_module(script_path)
    jobs = []
    for job in module.collect_jobs():
        jobs.append({
            "company": "Amazon",
            "role": str(job["title"]),
            "url": str(job["url"]),
            "location": str(job.get("location", "")),
            "posted_date": str(job.get("posted_date", "")),
            "query": str(job.get("query", "")),
            "source_monitor": source_monitor,
            "ats_platform": "Amazon.jobs",
        })
    return jobs


def unique_jobs(jobs: list[Job]) -> list[Job]:
    seen: set[str] = set()
    unique: list[Job] = []
    for job in jobs:
        normalized = tracker.normalize_job_url(job["url"])
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(job)
    return unique


def build_tracker_row(job: Job) -> dict[str, str]:
    notes_parts = [
        "Amazon 2027 monitor",
        "MAANGO manual-only",
        f"location={job.get('location', '')}",
    ]
    if job.get("posted_date"):
        notes_parts.append(f"posted_date={job['posted_date']}")
    if job.get("query"):
        notes_parts.append(f"matched_query={job['query']}")
    if job.get("source_monitor"):
        notes_parts.append(f"source_monitor={job['source_monitor']}")
    return {
        "company": job["company"],
        "status": "Pending Manual Action",
        "role": job["role"],
        "salary": "",
        "date": "",
        "url": job["url"],
        "rejection": "N/A",
        "notes": "; ".join(notes_parts),
        "ats_platform": job.get("ats_platform", "Amazon.jobs"),
    }


def plan_sync(
    *,
    broad_jobs: list[Job],
    sde_jobs: list[Job],
    duplicate_checker: Callable[[str, str, str], bool],
) -> Plan:
    candidates = unique_jobs([*sde_jobs, *broad_jobs])
    to_append: list[dict[str, str]] = []
    skipped_duplicates: list[Job] = []
    for job in candidates:
        if duplicate_checker(job["company"], job["role"], job["url"]):
            skipped_duplicates.append(job)
            continue
        to_append.append(build_tracker_row(job))
    return {
        "to_append": to_append,
        "skipped_duplicates": skipped_duplicates,
    }


def apply_plan(
    plan: Plan,
    *,
    append_row: Callable[[dict[str, str]], dict],
    enqueue_job: Callable[..., dict | object],
) -> dict:
    appended = []
    enqueued = []
    for row in plan["to_append"]:
        appended_result = append_row(row)
        queue_result = enqueue_job(
            company=row["company"],
            role=row["role"],
            url=row["url"],
            ats_platform=row.get("ats_platform", "Amazon.jobs"),
        )
        appended.append({"row": row, "result": appended_result})
        enqueued.append({"row": row, "result": queue_result})
    return {
        "appended_count": len(appended),
        "enqueued_count": len(enqueued),
        "appended": appended,
        "enqueued": enqueued,
    }


def default_duplicate_checker_factory() -> Callable[[str, str, str], bool]:
    rows = tracker.fetch_rows()
    return lambda company, role, url: tracker.duplicate(rows, company, role, url) is not None


def default_append_row(row: dict[str, str]) -> dict:
    values = [
        row["company"],
        row["status"],
        row["role"],
        row["salary"],
        row["date"],
        row["url"],
        row["rejection"],
        row["notes"],
    ]
    return tracker.append_verified(values)


def default_enqueue_job_factory(queue_db: Path) -> Callable[..., dict]:
    queue = app_queue.ApplicationQueue(queue_db)

    def enqueue_job(*, company: str, role: str, url: str, ats_platform: str) -> dict:
        job = queue.enqueue(company=company, role=role, url=url, ats_platform=ats_platform)
        return {
            "id": job.id,
            "state": job.state,
            "company": job.company,
            "role": job.role,
            "url": job.url,
            "ats_platform": job.ats_platform,
        }

    return enqueue_job


def run(*, broad_script: Path, sde_script: Path, queue_db: Path, commit: bool, output: Path) -> dict:
    broad_jobs = collect_monitor_jobs(broad_script, "broad")
    sde_jobs = collect_monitor_jobs(sde_script, "sde")
    plan = plan_sync(
        broad_jobs=broad_jobs,
        sde_jobs=sde_jobs,
        duplicate_checker=default_duplicate_checker_factory(),
    )
    report: dict = {
        "broad_jobs": broad_jobs,
        "sde_jobs": sde_jobs,
        "plan": plan,
        "commit": commit,
    }
    if commit:
        report["applied"] = apply_plan(
            plan,
            append_row=default_append_row,
            enqueue_job=default_enqueue_job_factory(queue_db),
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Amazon 2027 monitor hits into tracker + queue.")
    parser.add_argument("--broad-script", default=str(DEFAULT_BROAD_SCRIPT))
    parser.add_argument("--sde-script", default=str(DEFAULT_SDE_SCRIPT))
    parser.add_argument("--queue-db", default=str(DEFAULT_QUEUE_DB))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--commit", action="store_true", help="Append verified tracker rows and enqueue jobs.")
    args = parser.parse_args()

    report = run(
        broad_script=Path(args.broad_script),
        sde_script=Path(args.sde_script),
        queue_db=Path(args.queue_db),
        commit=args.commit,
        output=Path(args.output),
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
