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
        "fixture",
        "greenhouse_confirmation.html",
        "https://job-boards.greenhouse.io/fixture/applications/GH-123/confirmation",
        {"company": "Fixture Company", "role": "Software Engineer Intern", "requisition": "GH-123"},
    ),
    (
        "workday",
        "fixture",
        "workday_confirmation.html",
        "https://fixture.wd1.myworkdayjobs.com/en-US/Careers/application/WD-12345/confirmation",
        {"company": "Fixture Company", "role": "Software Engineering Intern", "requisition": "WD-12345"},
    ),
    (
        "lever",
        "fixture",
        "lever_confirmation.html",
        "https://jobs.lever.co/fixture/123/confirmation",
        {"company": "Fixture", "role": "Data Scientist Intern", "requisition": "123"},
    ),
    (
        "oracle",
        "example",
        "oracle_confirmation.html",
        "https://careers.example.test/job/123/confirmation",
        {"company": "Oracle Fixture Company", "role": "Software Engineer Intern", "requisition": "123"},
    ),
    (
        "njoyn",
        "cgi",
        "njoyn_confirmation.html",
        "https://cgi.njoyn.com/xweb/confirmation?jobid=123",
        {"company": "CGI Fixture Company", "role": "Software Developer Intern", "requisition": "123"},
    ),
]


@pytest.mark.parametrize("platform,tenant,fixture_name,page_url,identity", SCENARIOS)
def test_learned_live_reader_reconciles_exact_confirmation_and_candidate_home(
    platform, tenant, fixture_name, page_url, identity
):
    import live_confirmation_reader

    class Page:
        def read_only_snapshot(self):
            return {
                "target_id": "target-1",
                "url": page_url,
                "html": (ROOT / "fixtures" / fixture_name).read_text(encoding="utf-8"),
                "read_only": True,
            }

        def read_candidate_applications(self):
            return [{"platform": platform, **identity, "state": "submitted", "submitted": True}]

    result = live_confirmation_reader.read_and_reconcile(
        page=Page(),
        platform=platform,
        tenant=tenant,
        target_id="target-1",
        expected_url=page_url,
        expected_identity=identity,
    )

    assert result["portal_confirmed"] is True
    assert result["safe_for_post_submit"] is True
    assert result["reader"] == {"platform": platform, "tenant": tenant, "verified": True}
    assert result["human_required"] == []
    assert "<html" not in json.dumps(result).casefold()


def test_live_reader_returns_stable_human_required_when_tenant_reader_is_unverified():
    import live_confirmation_reader

    platform, _, fixture_name, page_url, identity = SCENARIOS[0]

    class Page:
        def read_only_snapshot(self):
            return {
                "target_id": "target-1",
                "url": page_url,
                "html": (ROOT / "fixtures" / fixture_name).read_text(encoding="utf-8"),
                "read_only": True,
            }

    result = live_confirmation_reader.read_and_reconcile(
        page=Page(),
        platform=platform,
        tenant="unverified-tenant",
        target_id="target-1",
        expected_url=page_url,
        expected_identity=identity,
    )

    assert result["portal_confirmed"] is False
    assert result["safe_for_post_submit"] is False
    assert result["reader"]["verified"] is False
    assert result["human_required"] == [
        {
            "type": "candidate_home_reader_unverified",
            "reason": "tenant_lacks_verified_candidate_home_reader",
        }
    ]
