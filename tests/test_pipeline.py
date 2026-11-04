from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pipeline


def test_route_candidates_separates_manual_supported_and_unsupported():
    scan = {
        "auto_apply_queue": [
            {"company": "A", "role": "Software Engineer Intern", "url": "https://job-boards.greenhouse.io/a/jobs/1", "ats_platform": "Greenhouse"},
            {"company": "B", "role": "Data Engineer Intern", "url": "https://b.wd1.myworkdayjobs.com/job/2", "ats_platform": "Workday"},
            {"company": "C", "role": "Research Intern", "url": "https://jobs.example.com/3", "ats_platform": "Unknown"},
        ],
        "manual_only": [
            {"company": "Google", "role": "Software Engineer Intern", "url": "https://careers.google.com/4", "ats_platform": "Unknown"}
        ],
    }

    routed = pipeline.route_candidates(scan)

    assert [x["company"] for x in routed["greenhouse"]] == ["A"]
    assert [x["company"] for x in routed["workday"]] == ["B"]
    assert [x["company"] for x in routed["unsupported"]] == ["C"]
    assert [x["company"] for x in routed["manual_only"]] == ["Google"]


def test_submission_record_requires_confirmation_evidence():
    job = {"company": "A", "role": "Software Engineer Intern", "url": "https://example.com/1"}
    try:
        pipeline.submission_row(job, confirmation_url="", confirmation_text="")
    except ValueError as exc:
        assert "confirmation" in str(exc).lower()
    else:
        raise AssertionError("submission without confirmation must be rejected")


def test_submission_record_is_eight_columns_with_confirmation():
    job = {"company": "A", "role": "Software Engineer Intern", "url": "https://example.com/1", "salary": "$40/hr"}
    row = pipeline.submission_row(job, confirmation_url="https://example.com/confirmation", confirmation_text="Application received")
    assert len(row) == 8
    assert row[1] == "Submitted - Pending Response"
    assert "confirmation" in row[7].lower()


def test_submission_record_rejects_presubmit_greenhouse_fixture_text():
    job = {"company": "A", "role": "Software Engineer Intern", "url": "https://example.com/1"}
    fixture_text = (ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="confirmation"):
        pipeline.submission_row(
            job,
            confirmation_url="https://job-boards.greenhouse.io/example/jobs/1",
            confirmation_text=fixture_text,
        )


def test_submission_record_accepts_workday_confirmation_fixture_text():
    job = {"company": "A", "role": "Software Engineer Intern", "url": "https://example.com/1"}
    fixture_text = (ROOT / "fixtures" / "workday_confirmation.html").read_text(encoding="utf-8")

    row = pipeline.submission_row(
        job,
        confirmation_url="https://example.wd1.myworkdayjobs.com/en-US/apply/confirmation",
        confirmation_text=fixture_text,
    )

    assert row[1] == "Submitted - Pending Response"
    assert "we've received your application" in row[7].lower()
