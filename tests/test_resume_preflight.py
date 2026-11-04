from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import resume_preflight


def test_preflight_accepts_profile_selected_pdf_with_required_filename(tmp_path):
    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nverified resume content")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "resume": {
            "primary": str(resume),
            "required_application_filename": "Resume.pdf",
            "do_not_use_for_applications": [str(tmp_path / "July Resume.pdf")],
        }
    }), encoding="utf-8")

    result = resume_preflight.preflight_profile_resume(profile)

    assert result["path"] == str(resume)
    assert result["basename"] == "Resume.pdf"
    assert result["content_type"] == "application/pdf"
    assert result["verified"] is True


def test_preflight_rejects_non_pdf_content_even_with_expected_filename(tmp_path):
    resume = tmp_path / "Resume.pdf"
    resume.write_text("not actually a PDF", encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "resume": {
            "primary": str(resume),
            "required_application_filename": "Resume.pdf",
            "do_not_use_for_applications": [],
        }
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="not a PDF"):
        resume_preflight.preflight_profile_resume(profile)
