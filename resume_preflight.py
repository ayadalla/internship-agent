"""Fail-closed verification of the resume selected by an application profile."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


PDF_MAGIC = b"%PDF-"


def preflight_profile_resume(profile_path: Path | str) -> dict[str, Any]:
    """Return safe evidence that the profile-selected resume is an allowed PDF."""
    profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    resume = profile.get("resume", {})
    primary = resume.get("primary")
    required_filename = resume.get("required_application_filename")
    if not isinstance(primary, str) or not primary:
        raise ValueError("Profile resume.primary is required")
    if not isinstance(required_filename, str) or not required_filename:
        raise ValueError("Profile resume.required_application_filename is required")

    path = Path(primary).expanduser()
    prohibited = {str(Path(value).expanduser().resolve()) for value in resume.get("do_not_use_for_applications", [])}
    if str(path.resolve()) in prohibited:
        raise ValueError("Profile-selected resume is prohibited for applications")
    if path.name != required_filename:
        raise ValueError("Profile-selected resume filename does not match required application filename")
    if not path.is_file():
        raise ValueError("Profile-selected resume file does not exist")

    content = path.read_bytes()
    if not content.startswith(PDF_MAGIC):
        raise ValueError("Profile-selected resume is not a PDF")

    return {
        "path": str(path),
        "basename": path.name,
        "content_type": "application/pdf",
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "verified": True,
    }
