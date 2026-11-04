from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import greenhouse_handler


def test_inspect_application_fixture_inventories_fields_and_verifies_uploaded_resume():
    fixture_text = (ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8")

    result = greenhouse_handler.inspect_html(
        fixture_text,
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result["page_type"] == "application"
    assert result["page_url"] == "https://job-boards.greenhouse.io/example/jobs/123"
    assert result["company"] == "Fixture Company"
    assert result["role"] == "Software Engineer Intern"
    assert result["location"] == "United States"
    assert result["uploaded_resume_verified"] is True
    assert result["manual_gate"] is None
    assert [field["label"] for field in result["fields"]] == [
        "First Name",
        "Last Name",
        "Email",
        "Phone",
        "Resume",
        "Work authorization",
        "Sponsorship",
    ]
    assert result["fields"][4] == {
        "label": "Resume",
        "name": "resume",
        "type": "file",
        "required": True,
    }


def test_inspect_html_classifies_greenhouse_listing_with_apply_entrypoint():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1>"
        "<p>Fixture Company — United States</p>"
        "<a href='#app' class='button'>Apply for this job</a>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["page_type"] == "listing"
    assert result["entrypoint"] == {"apply_label": "Apply for this job"}
    assert result["safe_to_prepare"] is False


def test_inspect_html_binds_a_for_label_to_its_separate_greenhouse_control():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1>"
        "<label for='work_auth'>Work authorization</label>"
        "<select id='work_auth' name='work_auth' required><option>Yes</option></select>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["fields"] == [{
        "label": "Work authorization",
        "name": "work_auth",
        "type": "select",
        "required": True,
    }]


def test_inspect_html_fails_closed_on_greenhouse_email_verification_gate():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Please verify your email to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["manual_gate"] == {
        "type": "email_verification",
        "detail": "Email verification detected",
    }


def test_main_fails_closed_for_greenhouse_manual_gate_and_emits_json(capsys, tmp_path):
    fixture = tmp_path / "email-verification.html"
    fixture.write_text("<h1>Software Engineer Intern</h1><p>Please verify your email to continue.</p>", encoding="utf-8")

    exit_code = greenhouse_handler.main([
        str(fixture),
        "--page-url",
        "https://job-boards.greenhouse.io/example/jobs/123",
    ])

    payload = __import__("json").loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["safe_to_prepare"] is False
    assert payload["manual_gate"]["type"] == "email_verification"


def test_inspect_html_fails_closed_when_greenhouse_asks_to_confirm_email_address():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Confirm your email address to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "email_verification",
        "detail": "Email verification detected",
    }


def test_inspect_html_fails_closed_when_greenhouse_asks_to_verify_email_address():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Verify email address before you continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "email_verification",
        "detail": "Email verification detected",
    }


def test_inspect_html_fails_closed_on_greenhouse_assessment_gate():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Complete the required assessment to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "assessment",
        "detail": "Assessment detected",
    }


def test_inspect_html_fails_closed_when_greenhouse_requires_a_skills_test():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Please complete the skills test to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "assessment",
        "detail": "Assessment detected",
    }


def test_inspect_html_fails_closed_when_greenhouse_requires_an_online_evaluation():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Complete the online evaluation to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "assessment",
        "detail": "Assessment detected",
    }


def test_inspect_html_fails_closed_when_greenhouse_requires_a_coding_challenge():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Complete the coding challenge to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "assessment",
        "detail": "Assessment detected",
    }


def test_inspect_html_fails_closed_on_greenhouse_identity_verification_gate():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Identity verification is required to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "identity_verification",
        "detail": "Identity verification required",
    }


def test_inspect_html_fails_closed_when_greenhouse_asks_to_verify_identity():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1><p>Please verify your identity to continue.</p>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["safe_to_prepare"] is False
    assert result["manual_gate"] == {
        "type": "identity_verification",
        "detail": "Identity verification required",
    }


def test_inspect_html_ignores_invisible_recaptcha_plumbing_without_visible_challenge():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1>"
        "<input id='first_name'>"
        "<textarea name='g-recaptcha-response' hidden></textarea>"
        "<script>window.ENV = {GOOGLE_RECAPTCHA_INVISIBLE_KEY: 'fixture'};</script>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["page_type"] == "application"
    assert result["manual_gate"] is None
    assert result["safe_to_prepare"] is True


def test_inspect_html_reports_prefilled_controls_and_dynamic_education_selectors():
    result = greenhouse_handler.inspect_html(
        "<h1>Software Engineer Intern</h1>"
        "<input id='first_name' value='Kevin'>"
        "<input id='school--1' value=''>"
        "<input id='degree--1' value=''>"
        "<input id='discipline--1' value=''>"
        "<input id='start-month--1' value=''>"
        "<input id='start-year--1' value='2024'>"
        "<input id='end-month--1' value=''>"
        "<input id='end-year--1' value='2028'>"
        "<input id='office-choice' type='checkbox' value='open-to-all'>",
        page_url="https://job-boards.greenhouse.io/example/jobs/123",
    )

    assert result["control_hints"] == {
        "prefilled_ids": ["end-year--1", "first_name", "start-year--1"],
        "education_groups": [{
            "index": 1,
            "selectors": {
                "school": "#school--1",
                "degree": "#degree--1",
                "discipline": "#discipline--1",
                "start_month": "#start-month--1",
                "start_year": "#start-year--1",
                "end_month": "#end-month--1",
                "end_year": "#end-year--1",
            },
        }],
    }


def test_inspect_html_prefers_modern_greenhouse_document_title_for_company():
    result = greenhouse_handler.inspect_html(
        "<title>Job Application for Software Engineer - Intern (Summer 2027) "
        "at C3 AI Ascend Internship Program: Summer 2027</title>"
        "<h1>Software Engineer - Intern (Summer 2027)</h1>"
        "<p>Join our teams — shipping code that matters.</p>"
        "<input id='first_name'>",
        page_url="https://job-boards.greenhouse.io/c3ascend/jobs/8739036002",
    )

    assert result["company"] == "C3 AI Ascend Internship Program: Summer 2027"
