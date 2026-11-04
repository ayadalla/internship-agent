from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import lever_handler


def test_inspect_application_fixture_inventories_fields_and_verifies_uploaded_resume():
    fixture_text = (ROOT / "fixtures" / "lever_application.html").read_text(encoding="utf-8")

    result = lever_handler.inspect_html(
        fixture_text,
        page_url="https://jobs.lever.co/example/1/apply",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result["page_type"] == "application"
    assert result["company"] == "Example"
    assert result["role"] == "Data Scientist Intern"
    assert result["location"] == "San Diego, CA"
    assert result["uploaded_resume_verified"] is True
    assert result["manual_gate"] is None
    assert [field["label"] for field in result["fields"]] == [
        "Full name",
        "Email",
        "Resume/CV",
        "LinkedIn",
    ]
    assert result["fields"][2] == {
        "label": "Resume/CV",
        "name": "resume",
        "type": "file",
        "required": True,
    }



def test_inspect_manual_gate_fixture_detects_captcha_blocker():
    fixture_text = (ROOT / "fixtures" / "lever_manual_gate.html").read_text(encoding="utf-8")

    result = lever_handler.inspect_html(
        fixture_text,
        page_url="https://jobs.lever.co/example/1/apply",
    )

    assert result["page_type"] == "application"
    assert result["manual_gate"] == {"type": "captcha", "detail": "CAPTCHA challenge detected"}



def test_inspect_confirmation_fixture_uses_verified_confirmation_evidence():
    fixture_text = (ROOT / "fixtures" / "lever_confirmation.html").read_text(encoding="utf-8")

    result = lever_handler.inspect_html(
        fixture_text,
        page_url="https://jobs.lever.co/example/1/apply/confirm",
    )

    assert result["page_type"] == "confirmation"
    assert "received your application" in result["confirmation_text"].lower()
    assert result["manual_gate"] is None



def test_main_emits_machine_readable_json_for_fixture(capsys):
    exit_code = lever_handler.main([
        str(ROOT / "fixtures" / "lever_application.html"),
        "--page-url",
        "https://jobs.lever.co/example/1/apply",
        "--expected-resume-basename",
        "Kevin_Pyo_Resume.pdf",
    ])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["page_type"] == "application"
    assert payload["uploaded_resume_verified"] is True
    assert payload["fields"][2]["label"] == "Resume/CV"
