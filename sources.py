#!/usr/bin/env python3
"""Public job source adapters with deterministic normalization."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from source_registry import load_registry


DEFAULT_TIMEOUT = 15.0
DEFAULT_ATTEMPTS = 3
STALE_POSTING_DAYS = 30


class ResponseLike(Protocol):
    def read(self) -> bytes: ...
    def __enter__(self) -> "ResponseLike": ...
    def __exit__(self, exc_type, exc, tb) -> bool | None: ...


OpenUrl = Callable[[str, float], ResponseLike]


def _default_open(url: str, timeout: float) -> ResponseLike:
    return urlopen(url, timeout=timeout)


def _company_name(token: str) -> str:
    return token.replace("-", " ").title()


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    keep = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "trk", "trackingid", "gh_src"}
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(keep), ""))


def _dedupe_jobs(jobs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique: list[dict] = []
    for job in sorted(jobs, key=lambda item: (item.get("company", ""), item.get("role", ""), _normalize_url(item.get("url", "")))):
        normalized_url = _normalize_url(job.get("url", ""))
        if not normalized_url or normalized_url in seen:
            continue
        seen.add(normalized_url)
        unique.append({**job, "url": normalized_url})
    return unique


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _latest_posting_at(jobs: list[dict]) -> str | None:
    timestamps = [
        parsed
        for job in jobs
        for parsed in (_parse_timestamp(job.get("updated_at")), _parse_timestamp(job.get("created_at")))
        if parsed is not None
    ]
    if not timestamps:
        return None
    return max(timestamps).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stale_warning(latest_posting_at: str | None) -> str | None:
    if latest_posting_at is None:
        return None
    latest_posting_dt = _parse_timestamp(latest_posting_at)
    assert latest_posting_dt is not None
    if latest_posting_dt <= _utcnow() - timedelta(days=STALE_POSTING_DAYS):
        return f"Newest posting timestamp is older than {STALE_POSTING_DAYS} days"
    return None


def _missing_timestamp_warning(jobs: list[dict]) -> str | None:
    if jobs and _latest_posting_at(jobs) is None:
        return "No posting timestamps available; freshness unknown"
    return None


def _aggregate_missing_timestamp_warning(source_runs: list[dict[str, Any]]) -> str | None:
    if any(run.get("freshness_unknown") for run in source_runs):
        return "One or more configured source runs succeeded without posting timestamps; freshness unknown"
    return None


def _freshness_summary(source_runs: list[dict[str, Any]]) -> dict[str, int]:
    stale_runs = sum(1 for run in source_runs if run.get("stale_result"))
    freshness_unknown_runs = sum(1 for run in source_runs if run.get("freshness_unknown"))
    error_runs = sum(1 for run in source_runs if run.get("status") == "error")
    healthy_runs = len(source_runs) - stale_runs - freshness_unknown_runs - error_runs
    return {
        "total_runs": len(source_runs),
        "healthy_runs": healthy_runs,
        "stale_runs": stale_runs,
        "freshness_unknown_runs": freshness_unknown_runs,
        "error_runs": error_runs,
    }



def _freshness_buckets(source_runs: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    buckets = {
        "healthy": [],
        "stale": [],
        "freshness_unknown": [],
        "error": [],
    }
    for run in source_runs:
        bucket = "healthy"
        if run.get("status") == "error":
            bucket = "error"
        elif run.get("freshness_unknown"):
            bucket = "freshness_unknown"
        elif run.get("stale_result"):
            bucket = "stale"
        buckets[bucket].append({"source": str(run["source"]), "token": str(run["token"])})
    for key in buckets:
        buckets[key].sort(key=lambda item: (item["source"], item["token"]))
    return buckets


def _source_health_status(*, failures: list[dict[str, Any]], source_runs: list[dict[str, Any]], candidate_count: int) -> str:
    if failures:
        return "partial_error"
    if candidate_count == 0:
        return "stale_or_unknown"
    if any(run.get("stale_result") or run.get("freshness_unknown") for run in source_runs):
        return "stale_or_unknown"
    return "healthy"


def _load_json(url: str, *, opener: OpenUrl, timeout: float = DEFAULT_TIMEOUT, attempts: int = DEFAULT_ATTEMPTS) -> Any:
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            with opener(url, timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            last_error = exc
    assert last_error is not None
    raise RuntimeError(f"Failed to fetch {url} after {max(1, attempts)} attempts") from last_error


def fetch_greenhouse_jobs(board_token: str, *, opener: OpenUrl = _default_open, attempts: int = DEFAULT_ATTEMPTS) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
    payload = _load_json(url, opener=opener, attempts=attempts)

    jobs: list[dict] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Invalid Greenhouse feed: expected a jobs list")
    for job in payload["jobs"]:
        role = (job.get("title") or "").strip()
        jobs.append(
            {
                "company": _company_name(board_token),
                "role": role,
                "url": job.get("absolute_url", "").strip(),
                "location": (job.get("location") or {}).get("name", "").strip(),
                "source": "Greenhouse public API",
                "updated_at": (job.get("updated_at") or "").strip(),
            }
        )
    return jobs


def fetch_lever_jobs(company_token: str, *, opener: OpenUrl = _default_open, attempts: int = DEFAULT_ATTEMPTS) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{company_token}?mode=json"
    payload = _load_json(url, opener=opener, attempts=attempts)

    jobs: list[dict] = []
    for job in payload:
        role = (job.get("text") or "").strip()
        categories = job.get("categories") or {}
        jobs.append(
            {
                "company": _company_name(company_token),
                "role": role,
                "url": job.get("hostedUrl", "").strip(),
                "location": (categories.get("location") or "").strip(),
                "team": (categories.get("team") or "").strip(),
                "source": "Lever public API",
                "created_at": job.get("createdAt"),
            }
        )
    return jobs


def fetch_ashby_jobs(board_token: str, *, opener: OpenUrl = _default_open, attempts: int = DEFAULT_ATTEMPTS) -> list[dict]:
    """Preserve official listing evidence; do not infer internship eligibility."""
    if not isinstance(board_token, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", board_token):
        raise ValueError("Ashby token must be an exact board identifier, not a URL or path")
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board_token}"
    payload = _load_json(url, opener=opener, attempts=attempts)
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("Invalid Ashby feed: expected a jobs list")
    jobs = []
    for job in payload["jobs"]:
        if job.get("isListed") is not True:
            continue
        if any(not isinstance(job.get(key), str) or not job[key].strip()
               for key in ("id", "title", "jobUrl")):
            raise ValueError("Invalid Ashby feed: listed job needs id, title and jobUrl")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job["id"]):
            raise ValueError("Invalid Ashby feed: posting id must be a single path segment of ASCII letters, digits, _ or -")
        try:
            posting_url = urlsplit(job["jobUrl"])
            if (posting_url.scheme != "https" or posting_url.netloc != "jobs.ashbyhq.com"
                    or posting_url.path.rstrip("/") != f"/{board_token}/{job['id']}"):
                raise ValueError("expected exact board/posting identity")
        except ValueError as exc:
            raise ValueError("Invalid Ashby feed: jobUrl must match its official HTTPS board and posting id") from exc
        secondary = job.get("secondaryLocations") or []
        locations = [job.get("location") or ""] + [item.get("location") or "" for item in secondary]
        jobs.append({
            "company": _company_name(board_token), "role": job.get("title") or "",
            "posting_id": job.get("id"), "url": job.get("jobUrl") or "",
            "location": "; ".join(dict.fromkeys(value for value in locations if value)),
            "address": job.get("address"), "secondary_locations": secondary,
            "employment_type": job.get("employmentType"), "is_listed": job.get("isListed"),
            "description_plain": job.get("descriptionPlain") or "",
            "description_html": job.get("descriptionHtml") or "",
            "created_at": job.get("publishedAt"), "source": "Ashby public API",
        })
    return jobs


def discover_jobs(*, greenhouse=(), lever=(), ashby=(), registry_path: str | Path | None = None,
                  opener: OpenUrl | None = None,
                  attempts: int = DEFAULT_ATTEMPTS) -> dict[str, Any]:
    """Fetch official public feeds without browser, accounts, Sheets, or output files.

    Returns {jobs, report, exit_code}; jobs are normalized source postings, NOT
    verified eligibility, current application state, or submission permission.
    Call orchestrator.build_scan(jobs, profile, known_urls=...) next. Errors,
    empty feeds, and unknown/stale timestamps are retained in report. Inject
    opener(url, timeout) to replay offline snapshots; no synthetic fallback.
    """
    jobs: list[dict] = []
    failures: list[dict] = []
    source_runs: list[dict] = []
    greenhouse, lever, ashby = list(greenhouse), list(lever), list(ashby)
    if registry_path is not None:
        if greenhouse or lever or ashby:
            raise ValueError("registry_path cannot be combined with explicit source tokens")
        registry = load_registry(registry_path)
        greenhouse = [entry["token"] for entry in registry["sources"] if entry["platform"] == "greenhouse"]
        lever = [entry["token"] for entry in registry["sources"] if entry["platform"] == "lever"]
    for platform, tokens, fetcher in (("greenhouse", greenhouse, fetch_greenhouse_jobs),
                                      ("lever", lever, fetch_lever_jobs),
                                      ("ashby", ashby, fetch_ashby_jobs)):
        for token in tokens:
            try:
                kwargs: dict[str, Any] = {"attempts": attempts}
                if opener is not None:
                    kwargs["opener"] = opener
                token_jobs = fetcher(token, **kwargs)
                latest = _latest_posting_at(token_jobs)
                run = {"source": platform, "token": token, "status": "ok", "candidates": len(token_jobs)}
                if latest is not None:
                    run["latest_posting_at"] = latest
                    warning = _stale_warning(latest)
                    if warning:
                        run.update(warning=warning, stale_result=True)
                else:
                    warning = _missing_timestamp_warning(token_jobs)
                    if warning:
                        run.update(warning=warning, freshness_unknown=True)
                jobs.extend(token_jobs)
                source_runs.append(run)
            except Exception as exc:
                failure = {"source": platform, "token": token, "error": str(exc)}
                failures.append(failure)
                source_runs.append({**failure, "status": "error", "candidates": 0})

    unique_jobs = _dedupe_jobs(jobs)
    latest = _latest_posting_at(unique_jobs)
    report = {
        "greenhouse_tokens": greenhouse, "lever_tokens": lever,
        **({"ashby_tokens": ashby} if ashby else {}),
        "candidates": len(unique_jobs), "failures": failures, "source_runs": source_runs,
        "source_health_status": _source_health_status(failures=failures, source_runs=source_runs,
                                                       candidate_count=len(unique_jobs)),
        "freshness_summary": _freshness_summary(source_runs),
        "freshness_buckets": _freshness_buckets(source_runs),
    }
    if not greenhouse and not lever and not ashby:
        report.update(source_health_status="partial_error",
                      error="At least one --greenhouse or --lever token is required")
        return {"jobs": [], "report": report, "exit_code": 2}
    if latest is not None:
        report["latest_posting_at"] = latest
    if not failures and not unique_jobs:
        report.update(warning="Configured source tokens returned zero job postings", stale_result=True)
    elif not failures:
        warning = _aggregate_missing_timestamp_warning(source_runs)
        if warning:
            report.update(freshness_unknown=True, warning=warning, stale_result=True)
        elif latest is not None:
            warning = _stale_warning(latest)
            if warning:
                report.update(warning=warning, stale_result=True)
    return {"jobs": unique_jobs, "report": report,
            "exit_code": 1 if failures else 3 if report.get("stale_result") else 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--greenhouse", action="append", default=[], help="Greenhouse board token")
    parser.add_argument("--lever", action="append", default=[], help="Lever company token")
    parser.add_argument("--ashby", action="append", default=[], help="Ashby public job-board token (explicit sources only)")
    parser.add_argument("--ashby-snapshot", help="Replay saved Ashby public JSON without network or live verification")
    parser.add_argument("--registry", help="Versioned approved-source registry JSON path")
    parser.add_argument("--output", required=True, help="Output JSON array path")
    parser.add_argument("--report", help="Optional machine-readable source health report path")
    args = parser.parse_args(argv)

    snapshot_requested = args.ashby_snapshot is not None
    if snapshot_requested and args.ashby_snapshot == "":
        parser.error("--ashby-snapshot requires a nonempty file path")
    if snapshot_requested and (len(args.ashby) != 1 or args.greenhouse or args.lever or args.registry):
        parser.error("--ashby-snapshot requires exactly one --ashby and no other sources or registry")
    if snapshot_requested:
        snapshot = Path(args.ashby_snapshot)
        for value in (args.output, args.report):
            if value:
                destination = Path(value)
                if (destination.resolve() == snapshot.resolve()
                        or (destination.exists() and snapshot.exists() and destination.samefile(snapshot))):
                    parser.error("snapshot input must not be an output or report destination")

    if args.registry:
        if args.greenhouse or args.lever or args.ashby:
            parser.error("--registry cannot be combined with --greenhouse or --lever")
        registry = load_registry(args.registry)
        args.greenhouse = [entry["token"] for entry in registry["sources"] if entry["platform"] == "greenhouse"]
        args.lever = [entry["token"] for entry in registry["sources"] if entry["platform"] == "lever"]

    output_path = Path(args.output)
    if not args.greenhouse and not args.lever and not args.ashby:
        result = {
            "greenhouse_tokens": args.greenhouse,
            "lever_tokens": args.lever,
            "candidates": 0,
            "failures": [],
            "source_health_status": "partial_error",
            "freshness_summary": _freshness_summary([]),
            "freshness_buckets": _freshness_buckets([]),
            "output": str(output_path),
            "error": "At least one --greenhouse or --lever token is required",
        }
        if args.report:
            result["report"] = args.report
            Path(args.report).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result))
        return 2

    snapshot_evidence = None
    opener: OpenUrl | None = None
    if snapshot_requested:
        snapshot_evidence = {"kind": "saved_public_snapshot", "path": args.ashby_snapshot,
                             "sha256": None, "live_verified": False}
        def open_snapshot(url, timeout):
            raw = Path(args.ashby_snapshot).read_bytes()
            snapshot_evidence["sha256"] = hashlib.sha256(raw).hexdigest()
            return io.BytesIO(raw)
        opener = open_snapshot
    discovery = discover_jobs(greenhouse=args.greenhouse, lever=args.lever, ashby=args.ashby, opener=opener)
    if snapshot_evidence is not None:
        discovery["report"]["source_evidence"] = snapshot_evidence
        for job in discovery["jobs"]:
            job.update(source="Ashby saved public snapshot", source_live_verified=False)
    output_path.write_text(json.dumps(discovery["jobs"], indent=2) + "\n", encoding="utf-8")
    result = {**discovery["report"], "output": str(output_path)}
    if args.report:
        result["report"] = args.report
        Path(args.report).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return discovery["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
