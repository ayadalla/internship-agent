from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import njoyn_handler


def test_inspect_html_classifies_njoyn_listing_and_exposes_apply_entrypoint():
    fixture_text = (ROOT / "fixtures" / "njoyn_listing.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?job=fixture",
    )

    assert result == {
        "page_type": "listing",
        "surface": "listing",
        "page_url": "https://cgi.njoyn.com/corp/xweb/xweb.asp?job=fixture",
        "role": "Software Developer Intern",
        "company": "CGI Careers",
        "location": "San Diego, California",
        "fields": [],
        "entrypoint": {"apply_label": "Apply now", "apply_url": "/apply/fixture"},
        "uploaded_resume_verified": None,
        "parser_correction_required": False,
        "parser_mismatches": [],
        "manual_gate": None,
        "confirmation_text": None,
        "confirmation_reference_id": None,
        "safe_to_prepare": False,
    }


def test_inspect_html_inventories_njoyn_account_controls_and_fails_closed():
    fixture_text = (ROOT / "fixtures" / "njoyn_login_profile.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?login=fixture",
    )

    assert result["page_type"] == "account"
    assert result["surface"] == "account"
    assert result["fields"] == [
        {"name": "email", "type": "email", "label": "Email address", "required": False},
        {"name": "password", "type": "password", "label": "Password", "required": False},
    ]
    assert result["entrypoint"] == {"create_profile_label": "Create a profile"}
    assert result["manual_gate"] == {
        "type": "account_sign_in",
        "detail": "Sign in or create a profile required",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_inventories_njoyn_privacy_surface_and_fails_closed():
    fixture_text = (ROOT / "fixtures" / "njoyn_privacy.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?privacy=fixture",
    )

    assert result["page_type"] == "privacy"
    assert result["surface"] == "privacy"
    assert result["role"] == "Privacy Notice"
    assert result["fields"] == [
        {
            "name": "privacy_acknowledged",
            "type": "checkbox",
            "label": "I acknowledge the notice",
            "required": False,
        }
    ]
    assert result["manual_gate"] == {
        "type": "privacy_notice",
        "detail": "Privacy notice acknowledgement required",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_inventories_njoyn_disclosures_and_fails_closed():
    fixture_text = (ROOT / "fixtures" / "njoyn_disclosures.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?disclosures=fixture",
    )

    assert result["page_type"] == "disclosures"
    assert result["surface"] == "disclosures"
    assert result["role"] == "Employment disclosures"
    assert result["fields"] == [
        {"name": "authorized", "type": "radio", "label": "Yes", "required": False},
        {"name": "authorized", "type": "radio", "label": "No", "required": False},
        {"name": "sponsorship", "type": "radio", "label": "Yes", "required": False},
        {"name": "sponsorship", "type": "radio", "label": "No", "required": False},
    ]
    assert result["manual_gate"] == {
        "type": "employment_disclosures",
        "detail": "Required employment disclosures must be answered",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_inventories_njoyn_disability_surface_and_fails_closed():
    fixture_text = (ROOT / "fixtures" / "njoyn_disability.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?disability=fixture",
    )

    assert result["page_type"] == "disability"
    assert result["surface"] == "disability"
    assert result["role"] == "Voluntary Self-Identification of Disability"
    assert result["fields"] == [
        {
            "name": "disability",
            "type": "select",
            "label": "",  # No associated label; never substitute a page heading.
            "required": False,
        }
    ]
    assert result["manual_gate"] == {
        "type": "disability_disclosure",
        "detail": "Voluntary disability disclosure must be explicitly handled",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_inventories_njoyn_resume_upload_and_verifies_attached_filename():
    fixture_text = (ROOT / "fixtures" / "njoyn_resume_upload.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?resume=fixture",
        expected_resume_basename="Resume.pdf",
    )

    assert result["page_type"] == "resume_upload"
    assert result["surface"] == "resume-upload"
    assert result["role"] == "Resume upload"
    assert result["fields"] == [
        {"name": "resume", "type": "file", "label": "Resume", "required": False},
    ]
    assert result["uploaded_resume_verified"] is True
    assert result["manual_gate"] is None
    assert result["safe_to_prepare"] is False


def test_inspect_html_reports_explicit_njoyn_parsed_profile_mismatches_for_correction():
    fixture_text = """<!doctype html><main data-surface=\"parsed-profile\">
      <h1>Parsed profile</h1>
      <p data-parser-mismatch=\"school\">Correct this imported school value.</p>
      <label>School <input name=\"school\" value=\"Incorrect University\"></label>
    </main>"""

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?parsed=fixture",
    )

    assert result["page_type"] == "parsed_profile"
    assert result["surface"] == "parsed-profile"
    assert result["fields"] == [
        {"name": "school", "type": "text", "label": "School", "required": False},
    ]
    assert result["parser_correction_required"] is True
    assert result["parser_mismatches"] == ["school"]
    assert result["manual_gate"] == {
        "type": "parser_correction",
        "detail": "Parsed profile contains explicit mismatches that require correction",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_requires_real_two_step_njoyn_referral_selection_state():
    fixture_text = (ROOT / "fixtures" / "njoyn_referral.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?referral=fixture",
    )

    assert result["page_type"] == "referral"
    assert result["fields"] == [
        {"name": "source", "type": "select", "label": "", "required": False},
        {"name": "source_detail", "type": "select", "label": "", "required": False},
    ]
    assert result["referral_selection"] == {
        "parent": None,
        "child": None,
        "verified": False,
    }
    assert result["manual_gate"] == {
        "type": "referral_selection",
        "detail": "Social Media and Instagram must be selected as real options",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_inventories_njoyn_questionnaire_and_fails_closed_on_required_questions():
    fixture_text = (ROOT / "fixtures" / "njoyn_questionnaire.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?questionnaire=fixture",
    )

    assert result["page_type"] == "questionnaire"
    assert result["surface"] == "questionnaire"
    assert result["fields"] == [
        {"name": "age", "type": "select", "label": "Are you at least 18 years old?", "required": False},
        {"name": "compensation", "type": "text", "label": "Desired hourly compensation", "required": False},
    ]
    assert result["manual_gate"] == {
        "type": "unknown_required_questions",
        "detail": "Required questionnaire answers must be resolved before preparation",
    }
    assert result["safe_to_prepare"] is False


def test_inspect_html_extracts_verified_njoyn_confirmation_reference_without_submit_inference():
    fixture_text = (ROOT / "fixtures" / "njoyn_confirmation.html").read_text(encoding="utf-8")

    result = njoyn_handler.inspect_html(
        fixture_text,
        page_url="https://cgi.njoyn.com/corp/xweb/xweb.asp?confirmation=fixture",
    )

    assert result["page_type"] == "confirmation"
    assert result["confirmation_reference_id"] == "CGI-FIXTURE-001"
    assert result["manual_gate"] is None
    assert result["safe_to_prepare"] is False
