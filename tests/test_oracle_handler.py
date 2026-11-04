from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import oracle_handler


def test_inspect_application_fixture_inventories_fields_and_selected_options():
    fixture_text = (ROOT / "fixtures" / "oracle_application.html").read_text(encoding="utf-8")

    result = oracle_handler.inspect_html(
        fixture_text,
        page_url="https://careers.example.com/job/123/apply",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result["page_type"] == "application"
    assert result["role"] == "Software Engineer Intern"
    assert result["location"] == "United States"
    assert result["uploaded_resume_verified"] is True
    assert result["country_valid"] is True
    assert result["salary_selected"] == "Open to discuss"
    assert result["issues"] == []
    assert [field["label"] for field in result["fields"]] == [
        "First Name",
        "Country",
        "Resume",
        "Salary Expectation",
    ]
    assert result["fields"][1] == {
        "label": "Country",
        "name": "country",
        "type": "combobox",
        "required": True,
        "value": "United States",
        "options": ["Canada", "United States"],
    }



def test_inspect_issue_fixture_reports_navigation_targets_and_invalid_country():
    fixture_text = (ROOT / "fixtures" / "oracle_issues.html").read_text(encoding="utf-8")

    result = oracle_handler.inspect_html(
        fixture_text,
        page_url="https://careers.example.com/job/123/apply",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result["page_type"] == "application"
    assert result["country_valid"] is False
    assert result["salary_selected"] == "$20/hour"
    assert result["issues"] == [
        {
            "message": "Country must be set to United States.",
            "target": "country",
        },
        {
            "message": "Please choose a salary expectation.",
            "target": "salary",
        },
    ]



def test_inspect_confirmation_fixture_uses_verified_confirmation_evidence():
    fixture_text = (ROOT / "fixtures" / "oracle_confirmation.html").read_text(encoding="utf-8")

    result = oracle_handler.inspect_html(
        fixture_text,
        page_url="https://careers.example.com/job/123/apply/confirmation",
    )

    assert result["page_type"] == "confirmation"
    assert "application submitted" in result["confirmation_text"].lower()
    assert result["issues"] == []



def test_main_emits_machine_readable_json_for_fixture(capsys):
    exit_code = oracle_handler.main([
        str(ROOT / "fixtures" / "oracle_application.html"),
        "--page-url",
        "https://careers.example.com/job/123/apply",
        "--expected-resume-basename",
        "Kevin_Pyo_Resume.pdf",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["page_type"] == "application"
    assert payload["country_valid"] is True
    assert payload["salary_selected"] == "Open to discuss"
