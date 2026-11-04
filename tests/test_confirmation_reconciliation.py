from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


SCENARIOS = [
    (
        "greenhouse",
        "greenhouse_confirmation.html",
        "https://job-boards.greenhouse.io/fixture/applications/GH-123/confirmation",
        {"company": "Fixture Company", "role": "Software Engineer Intern", "requisition": "GH-123"},
    ),
    (
        "workday",
        "workday_confirmation.html",
        "https://fixture.wd1.myworkdayjobs.com/en-US/Careers/application/WD-12345/confirmation",
        {"company": "Fixture Company", "role": "Software Engineering Intern", "requisition": "WD-12345"},
    ),
    (
        "lever",
        "lever_confirmation.html",
        "https://jobs.lever.co/fixture/123/confirmation",
        {"company": "Fixture", "role": "Data Scientist Intern", "requisition": "123"},
    ),
    (
        "oracle",
        "oracle_confirmation.html",
        "https://careers.example.test/job/123/confirmation",
        {"company": "Oracle Fixture Company", "role": "Software Engineer Intern", "requisition": "123"},
    ),
    (
        "njoyn",
        "njoyn_confirmation.html",
        "https://cgi.njoyn.com/xweb/confirmation?jobid=123",
        {"company": "CGI Fixture Company", "role": "Software Developer Intern", "requisition": "123"},
    ),
]


@pytest.mark.parametrize("platform,fixture_name,page_url,identity", SCENARIOS)
def test_learned_ats_confirmation_requires_exact_submitted_portal_readback(
    platform, fixture_name, page_url, identity
):
    import confirmation_reconciliation

    confirmation = confirmation_reconciliation.extract_confirmation(
        platform=platform,
        html_text=(ROOT / "fixtures" / fixture_name).read_text(encoding="utf-8"),
        page_url=page_url,
    )
    result = confirmation_reconciliation.reconcile_candidate_portal(
        confirmation=confirmation,
        expected_identity=identity,
        candidate_applications=[{
            "platform": platform,
            **identity,
            "state": "submitted",
            "submitted": True,
        }],
    )

    assert result["portal_confirmed"] is True
    assert result["safe_for_post_submit"] is True
    assert result["platform"] == platform
    assert result["identity"] == identity
    assert result["confirmation"]["submitted"] is True
    assert len(result["confirmation"]["text_sha256"]) == 64
    assert result["portal_readback"] == {
        "matched_application_count": 1,
        "state": "submitted",
        "submitted": True,
        "verified": True,
    }
    assert result["human_required"] == []
    assert "<html" not in json.dumps(result).casefold()


def test_portal_reconciliation_preserves_identity_state_and_confirmation_drift_as_blockers():
    import confirmation_reconciliation

    platform, fixture_name, page_url, identity = SCENARIOS[2]
    confirmation = confirmation_reconciliation.extract_confirmation(
        platform=platform,
        html_text=(ROOT / "fixtures" / fixture_name).read_text(encoding="utf-8"),
        page_url=page_url,
    )
    confirmation["confirmation_url"] = "https://jobs.lever.co/fixture/other/confirmation"

    result = confirmation_reconciliation.reconcile_candidate_portal(
        confirmation=confirmation,
        expected_identity=identity,
        candidate_applications=[
            {
                "platform": platform,
                **identity,
                "state": "in_progress",
                "submitted": False,
            },
            {
                "platform": platform,
                "company": identity["company"],
                "role": "Different role",
                "requisition": identity["requisition"],
                "state": "submitted",
                "submitted": True,
            },
        ],
    )

    assert result["portal_confirmed"] is False
    assert result["safe_for_post_submit"] is False
    assert {item["type"] for item in result["human_required"]} == {
        "confirmation_requisition_mismatch",
        "portal_identity_mismatch",
        "portal_state_not_submitted",
    }
