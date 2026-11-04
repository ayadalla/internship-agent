"""Synthetic Ashby schema regressions prompted by official public pilot records.

These are not employer leads, filled applications, or eligibility evidence.
"""
import json
from datetime import datetime, timezone

import pytest

import sources
from test_sources import FakeResponse


def test_cli_discovers_listed_ashby_intern_with_secondary_us_location(tmp_path, monkeypatch):
    raw = {"jobs": [{
        "id": "synthetic-id", "title": "Software Engineer Intern (Winter 2027)",
        "jobUrl": "https://jobs.ashbyhq.com/example.ai/synthetic-id", "isListed": True,
        "location": "Canada", "address": {"postalAddress": {"addressCountry": "Canada"}},
        "secondaryLocations": [{"location": "United States", "address": {
            "postalAddress": {"addressCountry": "United States", "addressLocality": "San Francisco"}}}],
        "employmentType": "FullTime", "publishedAt": "2026-09-05T00:00:00Z",
        "descriptionPlain": "Required: enrolled student. Bonus: tool experience.",
        "descriptionHtml": "<p>Required: enrolled student.</p><p>Bonus: tool experience.</p>",
    }]}
    calls = []
    def open_public(url, timeout):
        calls.append(url)
        return FakeResponse(raw)
    monkeypatch.setattr(sources, "urlopen", open_public)
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 9, 6, tzinfo=timezone.utc))
    output = tmp_path / "public.json"
    report = tmp_path / "health.json"
    assert sources.main(["--ashby", "example.ai", "--output", str(output), "--report", str(report)]) == 0
    assert calls == ["https://api.ashbyhq.com/posting-api/job-board/example.ai"]
    jobs = json.loads(output.read_text(encoding="utf-8"))
    assert len(jobs) == 1
    assert jobs[0]["role"] == raw["jobs"][0]["title"]
    assert jobs[0]["posting_id"] == "synthetic-id"
    assert jobs[0]["location"] == "Canada; United States"
    assert jobs[0]["secondary_locations"] == raw["jobs"][0]["secondaryLocations"]
    assert jobs[0]["employment_type"] == "FullTime"
    assert jobs[0]["description_plain"] == raw["jobs"][0]["descriptionPlain"]
    assert jobs[0]["description_html"] == raw["jobs"][0]["descriptionHtml"]
    assert jobs[0]["source"] == "Ashby public API"
    assert jobs[0]["is_listed"] is True
    assert "submission_authorized" not in jobs[0]
    assert "eligible" not in jobs[0]
    assert json.loads(report.read_text(encoding="utf-8"))["ashby_tokens"] == ["example.ai"]


def test_ashby_discovery_excludes_rows_without_explicit_listed_true():
    raw = {"jobs": [{"id": str(index), "title": "Software Intern",
        "jobUrl": f"https://jobs.ashbyhq.com/example/{index}", "isListed": listed}
        for index, listed in enumerate([True, False, None, "true", 1])]}
    result = sources.discover_jobs(ashby=["example"], opener=lambda *args: FakeResponse(raw))
    assert [job["posting_id"] for job in result["jobs"]] == ["0"]
    assert result["report"]["source_runs"][0]["candidates"] == 1


@pytest.mark.parametrize("token", [" example.ai", "../example", "example/jobs", "example?x=1", "example#fragment", "https://example.com", ""])
def test_ashby_discovery_rejects_non_identifier_without_network(token):
    calls = []
    def opener(*args):
        calls.append(args)
        return FakeResponse({"jobs": []})
    result = sources.discover_jobs(ashby=[token], opener=opener)
    assert calls == []
    assert result["exit_code"] == 1
    assert "exact board identifier" in result["report"]["failures"][0]["error"]


@pytest.mark.parametrize("payload", [{"jobs": {}}, {"jobs": ""}, {"jobs": None},
    {"jobs": [{"isListed": True, "id": "1", "title": "Software Intern"}]},
    {"jobs": [{"isListed": True, "id": "1", "title": "Software Intern", "jobUrl": ""}]}]
    + [{"jobs": [{"isListed": True, "id": "1", "title": "Software Intern", "jobUrl": url}]}
       for url in ["#fragment", "https://["]])
def test_ashby_malformed_feed_is_an_error_not_a_successful_zero(payload):
    result = sources.discover_jobs(ashby=["example"], opener=lambda *args: FakeResponse(payload))
    assert result["exit_code"] == 1
    assert result["jobs"] == []
    assert result["report"]["source_runs"][0]["status"] == "error"
    assert "Invalid Ashby feed" in result["report"]["failures"][0]["error"]


def test_cli_replays_saved_ashby_bytes_without_claiming_live_discovery(tmp_path, monkeypatch):
    import hashlib
    payload = {"jobs": [{"id": "snapshot-id", "title": "Software Intern",
        "jobUrl": "https://jobs.ashbyhq.com/example/snapshot-id", "isListed": True,
        "location": "Canada", "secondaryLocations": [{"location": "United States"}]}]}
    snapshot = tmp_path / "official-saved.json"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "inspection.json"
    report = tmp_path / "report.json"
    monkeypatch.setattr(sources, "urlopen", lambda *args: pytest.fail("snapshot mode must not access network"))
    assert sources.main(["--ashby", "example", "--ashby-snapshot", str(snapshot),
                         "--output", str(output), "--report", str(report)]) == 3
    jobs = json.loads(output.read_text(encoding="utf-8"))
    assert jobs[0]["posting_id"] == "snapshot-id"
    assert jobs[0]["location"] == "Canada; United States"
    assert jobs[0]["source"] == "Ashby saved public snapshot"
    assert jobs[0]["source_live_verified"] is False
    evidence = json.loads(report.read_text(encoding="utf-8"))["source_evidence"]
    assert evidence == {"kind": "saved_public_snapshot", "path": str(snapshot),
        "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(), "live_verified": False}


@pytest.mark.parametrize("source_args", [[], ["--ashby", "a", "--ashby", "b"],
    ["--ashby", "a", "--greenhouse", "b"], ["--ashby", "a", "--lever", "b"]])
def test_snapshot_cli_requires_exactly_one_ashby_board(source_args, tmp_path):
    snapshot = tmp_path / "saved.json"
    snapshot.write_text('{"jobs": []}', encoding="utf-8")
    output = tmp_path / "output.json"
    with pytest.raises(SystemExit) as error:
        sources.main(source_args + ["--ashby-snapshot", str(snapshot), "--output", str(output)])
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("destination", ["output", "report"])
@pytest.mark.parametrize("alias_kind", ["direct", "hardlink"])
def test_snapshot_cli_never_overwrites_its_input(destination, alias_kind, tmp_path):
    snapshot = tmp_path / "saved.json"
    original = b'{"jobs": []}'
    snapshot.write_bytes(original)
    alias = snapshot
    if alias_kind == "hardlink":
        alias = tmp_path / "alias.json"
        alias.hardlink_to(snapshot)
    arguments = ["--ashby", "example", "--ashby-snapshot", str(snapshot),
                 "--output", str(alias if destination == "output" else tmp_path / "output.json")]
    if destination == "report":
        arguments += ["--report", str(alias)]
    with pytest.raises(SystemExit) as error:
        sources.main(arguments)
    assert error.value.code == 2
    assert snapshot.read_bytes() == original


@pytest.mark.parametrize("job_url", ["https://jobs.ashbyhq.com/other/1",
    "https://not-ashby.invalid/example/1", "https://jobs.ashbyhq.com/example/2",
    "http://jobs.ashbyhq.com/example/1"])
def test_ashby_listing_identity_matches_declared_board_and_posting(job_url):
    payload = {"jobs": [{"id": "1", "title": "Software Intern", "isListed": True, "jobUrl": job_url}]}
    result = sources.discover_jobs(ashby=["example"], opener=lambda *args: FakeResponse(payload))
    assert result["exit_code"] == 1
    assert result["jobs"] == []
    assert "Invalid Ashby feed" in result["report"]["failures"][0]["error"]
