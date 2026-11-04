from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import workday_handler


def test_inspect_application_fixture_inventories_wizard_and_verifies_resume():
    fixture_text = (ROOT / "fixtures" / "workday.html").read_text(encoding="utf-8")

    result = workday_handler.inspect_html(
        fixture_text,
        page_url="https://example.wd1.myworkdayjobs.com/en-US/careers/job/Software-Engineering-Intern_R123",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result["page_type"] == "application"
    assert result["tenant"] == "example.wd1.myworkdayjobs.com"
    assert result["role"] == "Software Engineering Intern — Workday Fixture"
    assert result["steps"] == [
        "My Information",
        "My Experience",
        "Application Questions",
        "Review",
    ]
    assert result["uploaded_resume_verified"] is True
    assert result["manual_gate"] is None
    assert [field["label"] for field in result["fields"]] == [
        "Email",
        "Phone",
        "Resume",
        "School",
        "Authorized",
        "Sponsorship",
    ]
    assert result["fields"][2] == {
        "label": "Resume",
        "name": "wd_resume",
        "type": "file",
        "required": True,
    }


def test_inspect_live_entrypoint_fixture_detects_listing_and_apply_metadata():
    fixture_text = (ROOT / "fixtures" / "workday_live_entrypoint.html").read_text(encoding="utf-8")

    result = workday_handler.inspect_html(
        fixture_text,
        page_url="https://tencent.wd1.myworkdayjobs.com/en-US/Tencent_Careers/job/Software-Engineering-Intern_R107162-1",
    )

    assert result["page_type"] == "listing"
    assert result["tenant"] == "tencent.wd1.myworkdayjobs.com"
    assert result["role"] == "Software Engineering Intern"
    assert result["location"] == "United Kingdom-London"
    assert result["entrypoint"] == {
        "apply_label": "Apply",
        "sign_in_label": "Sign In",
        "requisition_id": "R107162",
    }
    assert result["fields"] == []


def test_inspect_parsed_resume_fixture_reports_mismatches_and_save_draft():
    fixture_text = (ROOT / "fixtures" / "workday_parse_issues.html").read_text(encoding="utf-8")

    result = workday_handler.inspect_html(
        fixture_text,
        page_url="https://example.wd1.myworkdayjobs.com/en-US/careers/job/1",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result["uploaded_resume_verified"] is True
    assert result["save_draft_available"] is True
    assert result["parse_issues"] == [
        {
            "section": "education",
            "field": "school",
            "parsed_value": "University of California Davis",
            "expected_value": "University of California, San Diego",
        },
        {
            "section": "education",
            "field": "gpa",
            "parsed_value": "3.3",
            "expected_value": "3.8",
        },
    ]


def test_inspect_manual_gate_fixture_reports_every_human_gate():
    fixture_text = (ROOT / "fixtures" / "workday_manual_gates.html").read_text(encoding="utf-8")

    result = workday_handler.inspect_html(
        fixture_text,
        page_url="https://example.wd1.myworkdayjobs.com/en-US/careers/job/1",
    )

    assert result["manual_gate"] == {"type": "captcha", "detail": "CAPTCHA challenge detected"}
    assert result["manual_gates"] == [
        {"type": "captcha", "detail": "CAPTCHA challenge detected"},
        {"type": "email_verification", "detail": "Email verification required"},
        {"type": "assessment", "detail": "Assessment detected"},
    ]
    assert result["save_draft_available"] is True


def test_inspect_html_fails_closed_on_identity_verification_requirement():
    result = workday_handler.inspect_html(
        "<h1>Software Engineering Intern</h1><p>Verify your identity to continue.</p>",
        page_url="https://example.wd1.myworkdayjobs.com/en-US/careers/job/1",
    )

    assert result["manual_gates"] == [
        {"type": "identity_verification", "detail": "Identity verification required"}
    ]
    assert result["safe_to_prepare"] is False


def test_inspect_confirmation_fixture_requires_success_and_extracts_reference_id():
    fixture_text = (ROOT / "fixtures" / "workday_confirmation.html").read_text(encoding="utf-8")

    result = workday_handler.inspect_html(
        fixture_text,
        page_url="https://example.wd1.myworkdayjobs.com/en-US/candidate-home/submitted",
    )

    assert result["page_type"] == "confirmation"
    assert "received your application" in result["confirmation_text"].lower()
    assert result["confirmation_reference_id"] == "WD-12345"
    assert result["manual_gate"] is None


def test_main_fails_closed_for_manual_gates_and_emits_json(capsys):
    exit_code = workday_handler.main([
        str(ROOT / "fixtures" / "workday_manual_gates.html"),
        "--page-url",
        "https://example.wd1.myworkdayjobs.com/en-US/careers/job/1",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["safe_to_prepare"] is False
    assert [gate["type"] for gate in payload["manual_gates"]] == [
        "captcha",
        "email_verification",
        "assessment",
    ]


def test_inspect_start_application_fixture_exposes_safe_non_submitting_actions():
    fixture_text = (ROOT / "fixtures" / "workday_start_application.html").read_text(encoding="utf-8")

    result = workday_handler.inspect_html(
        fixture_text,
        page_url="https://example.wd1.myworkdayjobs.com/en-US/careers/job/1/apply",
    )

    assert result["page_type"] == "application_start"
    assert result["role"] == "Software Engineering Intern – Summer 2027"
    assert result["start_actions"] == {
        "autofill_with_resume": "/job/1/apply/autofillWithResume",
        "apply_manually": "/job/1/apply/applyManually",
        "use_last_application": "/job/1/apply/useMyLastApplication",
    }
    assert result["safe_to_prepare"] is False
