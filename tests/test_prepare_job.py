from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import prepare_job
import tenant_metadata


def test_prepare_saved_html_dispatches_to_supported_ats_handlers():
    scenarios = [
        {
            "fixture": "greenhouse.html",
            "page_url": "https://job-boards.greenhouse.io/example/jobs/123",
            "expected_resume_basename": "Kevin_Pyo_Resume.pdf",
            "platform": "greenhouse",
            "role": "Software Engineer Intern",
            "page_type": "application",
        },
        {
            "fixture": "workday.html",
            "page_url": "https://example.wd1.myworkdayjobs.com/en-US/careers/job/Software-Engineering-Intern_R123",
            "expected_resume_basename": "Kevin_Pyo_Resume.pdf",
            "platform": "workday",
            "role": "Software Engineering Intern — Workday Fixture",
            "page_type": "application",
        },
        {
            "fixture": "lever_application.html",
            "page_url": "https://jobs.lever.co/example/1/apply",
            "expected_resume_basename": "Kevin_Pyo_Resume.pdf",
            "platform": "lever",
            "role": "Data Scientist Intern",
            "page_type": "application",
        },
        {
            "fixture": "oracle_application.html",
            "page_url": "https://careers.example.com/job/123/apply",
            "expected_resume_basename": "Kevin_Pyo_Resume.pdf",
            "platform": "oracle",
            "role": "Software Engineer Intern",
            "page_type": "application",
        },
    ]

    for scenario in scenarios:
        result = prepare_job.prepare_saved_html(
            html_text=(ROOT / "fixtures" / scenario["fixture"]).read_text(encoding="utf-8"),
            page_url=scenario["page_url"],
            expected_resume_basename=scenario["expected_resume_basename"],
        )

        assert result["platform"] == scenario["platform"]
        assert result["page_type"] == scenario["page_type"]
        assert result["role"] == scenario["role"]
        assert result["submission_enabled"] is False
        assert result["page_url"] == scenario["page_url"]


def test_prepare_saved_html_dispatches_njoyn_listing_without_enabling_submission():
    page_url = "https://cgi.njoyn.com/corp/xweb/xweb.asp?job=fixture"

    result = prepare_job.prepare_saved_html(
        html_text=(ROOT / "fixtures" / "njoyn_listing.html").read_text(encoding="utf-8"),
        page_url=page_url,
    )

    assert result["platform"] == "njoyn"
    assert result["page_type"] == "listing"
    assert result["entrypoint"] == {
        "apply_label": "Apply now",
        "apply_url": "/apply/fixture",
    }
    assert result["safe_to_prepare"] is False
    assert result["submission_enabled"] is False
    assert result["page_url"] == page_url


def test_prepare_saved_html_reuses_authenticated_learned_tenant_session_without_account_creation():
    page_url = "https://cgi.njoyn.com/corp/xweb/xweb.asp?job=fixture"

    result = prepare_job.prepare_saved_html(
        html_text=(ROOT / "fixtures" / "njoyn_listing.html").read_text(encoding="utf-8"),
        page_url=page_url,
        tenant_metadata={
            "tenant": "cgi",
            "platform": "njoyn",
            "authenticated": True,
            "session_reference": "runtime-only:njoyn-cgi",
        },
    )

    assert result["tenant_session"] == {
        "tenant": "cgi",
        "authenticated": True,
        "reuse_authenticated_session": True,
        "account_creation_required": False,
        "session_reference": "runtime-only:njoyn-cgi",
    }
    assert result["submission_enabled"] is False


def test_load_tenant_metadata_returns_matching_authenticated_njoyn_tenant_only(tmp_path):
    metadata_path = tmp_path / "learned-tenants.json"
    metadata_path.write_text(json.dumps({
        "version": 1,
        "tenants": [{
            "tenant": "cgi",
            "platform": "njoyn",
            "hostname": "cgi.njoyn.com",
            "authenticated": True,
            "session_reference": "runtime-only:njoyn-cgi",
        }],
    }), encoding="utf-8")

    result = tenant_metadata.load_for_page(
        metadata_path,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?job=fixture",
        platform="njoyn",
    )

    assert result == {
        "tenant": "cgi",
        "platform": "njoyn",
        "authenticated": True,
        "session_reference": "runtime-only:njoyn-cgi",
    }


def test_main_loads_validated_tenant_metadata_without_serializing_secrets(tmp_path, capsys):
    metadata_path = tmp_path / "learned-tenants.json"
    metadata_path.write_text(json.dumps({
        "version": 1,
        "tenants": [{
            "tenant": "cgi",
            "platform": "njoyn",
            "hostname": "cgi.njoyn.com",
            "authenticated": True,
            "session_reference": "runtime-only:njoyn-cgi",
        }],
    }), encoding="utf-8")

    exit_code = prepare_job.main([
        str(ROOT / "fixtures" / "njoyn_listing.html"),
        "--page-url", "https://cgi.njoyn.com/corp/xweb/xweb.asp?job=fixture",
        "--tenant-metadata", str(metadata_path),
    ])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["submission_enabled"] is False
    assert payload["tenant_session"] == {
        "tenant": "cgi",
        "authenticated": True,
        "reuse_authenticated_session": True,
        "account_creation_required": False,
        "session_reference": "runtime-only:njoyn-cgi",
    }
    assert "secret" not in json.dumps(payload).casefold()


def test_main_uses_profile_resume_preflight_and_embeds_safe_evidence(tmp_path, capsys):
    resume = tmp_path / "Kevin_Pyo_Resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nresume")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "resume": {
            "primary": str(resume),
            "required_application_filename": "Kevin_Pyo_Resume.pdf",
            "do_not_use_for_applications": [],
        }
    }), encoding="utf-8")

    exit_code = prepare_job.main([
        str(ROOT / "fixtures" / "greenhouse.html"),
        "--page-url", "https://job-boards.greenhouse.io/example/jobs/123",
        "--profile", str(profile),
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["resume_preflight"] == {
        "basename": "Kevin_Pyo_Resume.pdf",
        "content_type": "application/pdf",
        "path": str(resume),
        "sha256": __import__("hashlib").sha256(resume.read_bytes()).hexdigest(),
        "size_bytes": len(resume.read_bytes()),
        "verified": True,
    }
    assert payload["resume_verified"] is True


def test_main_fails_closed_with_machine_readable_error_for_unsupported_ats(tmp_path, capsys):
    output_path = tmp_path / "prepare-job.json"

    exit_code = prepare_job.main([
        str(ROOT / "fixtures" / "greenhouse.html"),
        "--page-url",
        "https://example.com/custom/apply",
        "--output",
        str(output_path),
    ])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": "Unsupported ATS for URL: https://example.com/custom/apply",
        "page_url": "https://example.com/custom/apply",
        "submission_enabled": False,
    }
    assert not output_path.exists()


def test_main_fails_closed_for_supported_ats_manual_gate(tmp_path, capsys):
    fixture = tmp_path / "manual-gate.html"
    fixture.write_text("<h1>Software Engineer Intern</h1><p>Please verify your email to continue.</p>", encoding="utf-8")

    exit_code = prepare_job.main([
        str(fixture),
        "--page-url",
        "https://job-boards.greenhouse.io/example/jobs/123",
    ])

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["submission_enabled"] is False
    assert payload["safe_to_prepare"] is False
    assert payload["manual_gate"] == {
        "type": "email_verification",
        "detail": "Email verification detected",
    }
