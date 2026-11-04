"""Offline discovery policy regression tests (never use accounts or Sheets)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scanner
import orchestrator


def profile():
    return {"preferences": {
        "target_roles": ["Software Engineer Intern"],
        "target_levels": ["Intern", "Co-op", "New Grad", "Entry Level", "Fellow"],
        "target_timelines": ["Fall 2026", "Winter 2027", "Spring 2027", "Summer 2027"],
        "location_preference": "Remote or anywhere in U.S.",
    }}


def job(**overrides):
    return {"company": "Example", "role": "Software Engineer Intern, Summer 2027",
            "url": "https://job-boards.greenhouse.io/example/jobs/1",
            "location": "New York, NY", **overrides}


def no_tracker(*args, **kwargs):
    pytest.fail("Default discovery must not invoke the Sheets tracker")


@pytest.mark.parametrize("entrypoint", [scanner.classify, orchestrator.build_scan])
def test_default_discovery_never_invokes_tracker(entrypoint, monkeypatch):
    monkeypatch.setattr(scanner, "tracker_duplicate", no_tracker)
    value = job() if entrypoint is scanner.classify else [job()]
    result = entrypoint(value, profile())
    classified = result if entrypoint is scanner.classify else result["all_results"][0]
    assert classified["duplicate"] is False
    assert classified["relevant"] is True


@pytest.mark.parametrize("entrypoint", [scanner.classify, orchestrator.build_scan])
def test_discovery_deduplicates_exact_local_urls(entrypoint, monkeypatch):
    monkeypatch.setattr(scanner, "tracker_duplicate", no_tracker)
    candidate = job(url=job()["url"] + "/?utm_source=test")
    value = candidate if entrypoint is scanner.classify else [candidate]
    result = entrypoint(value, profile(), known_urls={job()["url"]})
    classified = result if entrypoint is scanner.classify else result["all_results"][0]
    assert classified["duplicate"] is True
    other = job(url=job()["url"] + "2")
    other_value = other if entrypoint is scanner.classify else [other]
    other_result = entrypoint(other_value, profile(), known_urls={job()["url"]})
    other_classified = other_result if entrypoint is scanner.classify else other_result["all_results"][0]
    assert other_classified["duplicate"] is False


@pytest.mark.parametrize("entrypoint", [scanner.classify, orchestrator.build_scan])
def test_injected_exact_portal_duplicate_check_is_used_without_sheets(entrypoint, monkeypatch):
    monkeypatch.setattr(scanner, "tracker_duplicate", no_tracker)
    calls = []
    def portal_check(company, role, url):
        calls.append((company, role, url))
        return url == job()["url"]
    value = job() if entrypoint is scanner.classify else [job()]
    result = entrypoint(value, profile(), duplicate_checker=portal_check)
    classified = result if entrypoint is scanner.classify else result["all_results"][0]
    assert classified["duplicate"] is True
    assert calls == [(job()["company"], job()["role"], job()["url"])]


def test_tracker_duplicate_check_requires_explicit_opt_in(monkeypatch):
    calls = []
    monkeypatch.setattr(scanner, "tracker_duplicate", lambda *args: calls.append(args) or True)
    assert scanner.classify(job(), profile(), use_tracker=True)["duplicate"] is True
    assert len(calls) == 1


def test_orchestrator_uses_existing_queue_for_exact_dedup_without_sheets(tmp_path, monkeypatch):
    monkeypatch.setattr(scanner, "tracker_duplicate", no_tracker)
    resume = tmp_path / "resume.pdf"
    resume.write_text("synthetic test resume", encoding="utf-8")
    p = {**profile(), "name": {"full": "Test User"},
         "contact": {"email": "test@example.com", "phone": "555-1111"},
         "resume": {"primary": str(resume)}}
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(p), encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([job()]), encoding="utf-8")
    args = (candidates, profile_path, tmp_path / "report.json", tmp_path / "queue.db", tmp_path / "audit.jsonl")
    first = orchestrator.run(*args)
    second = orchestrator.run(*args)
    assert first["scan"]["new"] == 1
    assert second["scan"]["new"] == 0
    assert second["scan"]["all_results"][0]["duplicate"] is True
    assert first["queue"]["count"] == second["queue"]["count"] == 1


def test_scanner_cli_reads_local_queue_not_sheets(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(scanner, "tracker_duplicate", no_tracker)
    queue_path = tmp_path / "queue.sqlite3"
    queue = orchestrator.ApplicationQueue(queue_path)
    queue.enqueue(company=job()["company"], role=job()["role"], url=job()["url"], ats_platform="Greenhouse")
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile()), encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([job()]), encoding="utf-8")
    output = tmp_path / "scan.json"
    assert scanner.main([str(candidates), "--profile", str(profile_path), "--queue-db", str(queue_path),
                         "--output", str(output)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["new"] == 0
    assert result["all_results"][0]["duplicate"] is True


def test_local_queue_lookup_missing_is_read_only_and_corrupt_fails_closed(tmp_path):
    import sqlite3
    path = tmp_path / "queue.sqlite3"
    assert scanner.known_urls_from_queue(path) == set()
    assert not path.exists()
    path.write_bytes(b"corrupt sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        scanner.known_urls_from_queue(path)


@pytest.mark.parametrize("location", [
    "Austria", "Vienna, Austria", "Australia", "Sydney, Australia", "Toronto, ON",
    "Toronto, ON, Canada", "Remote - Canada only", "Remote (Australia)",
])
def test_foreign_location_is_not_allowed(location):
    assert scanner.location_allowed(location, profile()) is False
    result = scanner.classify(job(location=location), profile())
    assert result["relevant"] is False
    assert result["rejection_reasons"] == ["location:not_us_or_remote"]


@pytest.mark.parametrize("location", [
    "New York, NY", "Richmond, VA", "Palo Alto, California", "Boston, Massachusetts",
    "United States", "US", "USA", "U.S.", "Remote - United States", "Remote (US)",
    "Remote - Worldwide", "Remote - US or Canada",
])
def test_explicit_us_or_worldwide_remote_location_is_allowed(location):
    assert scanner.location_allowed(location, profile()) is True


@pytest.mark.parametrize("location", [None, "", "   ", "Remote", "Remote - North America", "TBD", "Various",
                                      "Business district", "Springfield, XX", "Georgia", "CA"])
def test_unknown_location_requires_verification_not_rejection(location):
    result = scanner.classify(job(location=location), profile())
    assert result["relevant"] is False
    assert result["rejection_reasons"] == []
    assert result["verification_reasons"] == ["location:needs_verification"]
    assert result["eligibility_status"] == "needs_verification"
    scan = orchestrator.build_scan([job(location=location)], profile())
    assert scan["auto_apply_queue"] == []
    assert scan["needs_verification"] == [result]


@pytest.mark.parametrize("location", ["Perth, WA, Australia", "London, ON, Canada", "Remote - Worldwide except US",
                                      "Remote - outside the United States", "Remote - Canada only (US team)"])
def test_explicit_foreign_restrictions_override_us_shaped_tokens(location):
    result = scanner.classify(job(location=location), profile())
    assert result["relevant"] is False
    assert result["rejection_reasons"] == ["location:not_us_or_remote"]
    assert result["eligibility_status"] == "ineligible"


def test_canonical_profile_accepts_winter_full_time_internship():
    profile_path = ROOT / "profile.json"
    if not profile_path.exists():
        pytest.skip("profile.json is personal and gitignored; this checks the owner's live profile")
    canonical = json.loads(profile_path.read_text(encoding="utf-8"))
    if "Winter 2027" not in canonical.get("preferences", {}).get("target_timelines", []):
        pytest.skip("this profile does not target Winter 2027")
    result = scanner.classify(job(role="Software Engineer Intern - Winter 2027 (Full-time)"), canonical)
    assert result["relevant"] is True


@pytest.mark.parametrize("role", ["Software Engineer Co-op", "Software Engineer Coop", "Software Engineer Co Op",
                                  "Software Engineer - New Grad", "Software Engineer - New-Grad",
                                  "Software Engineer - Entry Level", "Software Engineer - Entry-Level",
                                  "Software Engineer Internship", "Software Engineer Fellow"])
def test_classifier_preserves_configured_early_career_levels(role):
    assert scanner.classify(job(role=role), profile())["relevant"] is True


def test_classifier_honors_restricted_profile_levels():
    p = profile()
    p["preferences"]["target_levels"] = ["Intern"]
    assert scanner.classify(job(role="Software Engineer - New Grad"), p)["relevant"] is False


@pytest.mark.parametrize("role", ["Software Engineer, International Team", "Staff Software Engineer Intern Mentor",
                                  "Software Engineer - 2027", "Internal Software Engineer"])
def test_classifier_does_not_confuse_internal_or_senior_with_early_career(role):
    assert scanner.classify(job(role=role), profile())["relevant"] is False


@pytest.mark.parametrize("url, expected", [
    ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/123", "Oracle"),
    ("https://careers.americanexpress.com/en/sites/CX_1/job/26010970", "Oracle"),
    ("https://cgi.njoyn.com/CGI/xweb/xweb.asp?clid=21001&Jobid=J0926-0001", "Njoyn"),
    ("https://jobs.ashbyhq.com/example/123", "Ashby"),
    ("https://greenhouse.io.evil.invalid/jobs/1", "Unknown"),
    ("https://example.invalid/?next=https://jobs.ashbyhq.com/example/123", "Unknown"),
    ("https://example.invalid/greenhouse.io/jobs/1", "Unknown"),
])
def test_ats_detection_uses_real_host_or_oracle_custom_career_route(url, expected):
    assert scanner.detect_ats(url) == expected


@pytest.mark.parametrize("company", ["Amazon", "AWS", "Amazon Web Services"])
def test_amazon_summer_2027_is_priority_monitor_only_never_auto_queue(company):
    candidate = job(company=company, role="Software Development Engineer Internship - Summer 2027")
    result = scanner.classify(candidate, profile())
    assert result["relevant"] is True
    assert result["priority_monitor"] is True
    assert result["manual_only"] is True
    assert result["maango_parent"] == "Amazon"
    scan = orchestrator.build_scan([candidate], profile())
    assert scan["auto_apply_queue"] == []
    assert scan["manual_only"] == [result]


def test_non_amazon_or_non_summer_role_is_not_priority_monitor():
    assert scanner.classify(job(), profile())["priority_monitor"] is False
    assert scanner.classify(job(company="Amazon", role="Software Engineer Intern - Winter 2027"),
                            profile())["priority_monitor"] is False


@pytest.mark.parametrize("location", ["Remote - US time zones", "Remote (US hours)", "Remote - US team",
                                      "Remote - US company", "Remote - US-based team"])
def test_remote_us_company_or_working_hours_do_not_prove_residency_eligibility(location):
    result = scanner.classify(job(location=location), profile())
    assert result["eligibility_status"] == "needs_verification"
    assert result["rejection_reasons"] == []
    assert result["relevant"] is False


def test_greenhouse_tracking_variant_deduplicates_against_local_identity():
    result = scanner.classify(job(url=job()["url"] + "?gh_src=fixture"), profile(), known_urls=[job()["url"]])
    assert result["duplicate"] is True


@pytest.mark.parametrize("company, parent", [("AWS LLC", "Amazon"), ("Instagram", "Meta"),
                                             ("YouTube", "Google"), ("LinkedIn", "Microsoft"),
                                             ("Facebook", "Meta")])
def test_clear_maango_subsidiary_company_names_never_enter_auto_queue(company, parent):
    candidate = job(company=company)
    result = scanner.classify(candidate, profile())
    assert result["maango_parent"] == parent
    assert result["manual_only"] is True
    assert orchestrator.build_scan([candidate], profile())["auto_apply_queue"] == []
