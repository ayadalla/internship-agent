"""Offline contract fixtures only; none of these synthetic postings are live leads."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sources
import scanner
import orchestrator

from test_discovery_policy import profile
from test_sources import FakeResponse


@pytest.mark.parametrize("platform", ["greenhouse", "lever"])
def test_official_feed_does_not_discard_configured_career_levels_before_classification(platform):
    # In-memory source-schema snapshots, intentionally synthetic like test_sources.py.
    titles = ["Software Engineer Intern", "Software Engineer Co-op", "Software Engineer - New-Grad",
              "Software Engineer - Entry-Level", "Senior Software Engineer"]
    raw = []
    for index, title in enumerate(titles):
        url = f"https://jobs.example.invalid/{index}"
        raw.append({"id": index, "title": title, "text": title, "absolute_url": url, "hostedUrl": url,
                    "location": {"name": "United States"}, "categories": {"location": "United States"}})
    payload = {"jobs": raw} if platform == "greenhouse" else raw
    jobs = getattr(sources, f"fetch_{platform}_jobs")("example", opener=lambda *args: FakeResponse(payload))
    assert [job["role"] for job in jobs] == titles
    results = orchestrator.build_scan(jobs, profile())
    assert [job["role"] for job in results["all_results"] if job["relevant"]] == titles[:-1]


def test_existing_normalized_candidate_snapshot_replays_without_current_live_claims():
    # Historical repository artifact, not evidence those listings remain open today.
    snapshot = json.loads((ROOT / "verified-candidates.json").read_text(encoding="utf-8"))
    scan = orchestrator.build_scan(snapshot, profile())
    assert scan["scanned"] == 4
    assert scan["new"] == 4
    assert {job["company"] for job in scan["manual_only"]} == {"Google"}
    assert any(job["company"] == "Palantir Technologies" for job in scan["auto_apply_queue"])


@pytest.mark.parametrize("lever_fails", [False, True])
def test_callable_discovery_returns_official_feed_jobs_and_honest_health_without_files(lever_fails, monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
    calls = []
    def snapshot_open(url, timeout):
        calls.append(url)
        if "boards-api.greenhouse.io" in url:
            return FakeResponse({"jobs": [{"title": "Software Engineer Co-op",
                "absolute_url": "https://job-boards.greenhouse.io/example/jobs/1?gh_src=fixture",
                "location": {"name": "United States"}, "updated_at": "2026-09-04T00:00:00Z"}]})
        if lever_fails:
            raise TimeoutError("offline timeout fixture")
        return FakeResponse([{"text": "Software Engineer New Grad", "hostedUrl": "https://jobs.lever.co/example/2",
                             "categories": {"location": "United States"}, "createdAt": 1788480000000}])
    result = sources.discover_jobs(greenhouse=["example"], lever=["example"], opener=snapshot_open, attempts=1)
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true",
                     "https://api.lever.co/v0/postings/example?mode=json"]
    assert len(result["jobs"]) == (1 if lever_fails else 2)
    assert result["report"]["source_health_status"] == ("partial_error" if lever_fails else "healthy")
    assert result["exit_code"] == (1 if lever_fails else 0)
    assert bool(result["report"]["failures"]) is lever_fails
    assert "output" not in result["report"]
    assert result["jobs"][0]["url"] == "https://job-boards.greenhouse.io/example/jobs/1"


def test_callable_discovery_uses_approved_registry_once_per_source(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"version": 1, "sources": [
        {"platform": "greenhouse", "token": "example", "approved": True},
        {"platform": "greenhouse", "token": "example", "approved": True},
        {"platform": "lever", "token": "disabled", "approved": False},
    ]}), encoding="utf-8")
    calls = []
    def opener(url, timeout):
        calls.append(url)
        return FakeResponse({"jobs": []})
    result = sources.discover_jobs(registry_path=registry, opener=opener)
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"]
    assert result["jobs"] == []
    assert result["report"]["failures"] == []
    assert result["report"]["warning"] == "Configured source tokens returned zero job postings"
    assert result["report"]["source_runs"] == [
        {"source": "greenhouse", "token": "example", "status": "ok", "candidates": 0}]
    with pytest.raises(ValueError, match="cannot be combined"):
        sources.discover_jobs(registry_path=registry, greenhouse=["example"], opener=opener)


def test_unconfigured_callable_discovery_is_not_a_successful_empty_scan():
    result = sources.discover_jobs(opener=lambda *args: pytest.fail("must not fetch"))
    assert result["exit_code"] == 2
    assert result["report"]["source_health_status"] == "partial_error"
    assert result["report"]["error"]


@pytest.mark.parametrize("token", [" example ", "../other", "example?mode=html", "example/jobs", "https://example.com"])
def test_registry_rejects_malformed_tokens_without_normalizing_or_fetching(token, tmp_path):
    import source_registry
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"version": 1, "sources": [
        {"platform": "greenhouse", "token": token, "approved": True}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="token"):
        source_registry.load_registry(registry)


def test_greenhouse_error_payload_is_not_a_successful_empty_feed():
    result = sources.discover_jobs(greenhouse=["example"], opener=lambda *args: FakeResponse({"error": "not found"}))
    assert result["exit_code"] == 1
    assert result["report"]["source_runs"][0]["status"] == "error"
    assert result["jobs"] == []
