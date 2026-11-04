#!/usr/bin/env python3
"""Safety-focused router and submission recorder for the job agent."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
import re
from datetime import date
from pathlib import Path

BASE = Path.home() / "Documents/job-agent"


def route_candidates(scan: dict) -> dict[str, list[dict]]:
    routed: dict[str, list[dict]] = {
        "greenhouse": [],
        "workday": [],
        "unsupported": [],
        "manual_only": list(scan.get("manual_only", [])),
    }
    for job in scan.get("auto_apply_queue", []):
        ats = (job.get("ats_platform") or "").casefold()
        if ats == "greenhouse":
            routed["greenhouse"].append(job)
        elif ats == "workday":
            routed["workday"].append(job)
        else:
            routed["unsupported"].append(job)
    return routed


class _ConfirmationTextParser(HTMLParser):
    """Extract message text, never bootstrapped future-success script strings."""

    def __init__(self):
        super().__init__()
        self.in_script = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if not self.in_script:
            self.parts.append(data)


def normalize_confirmation_text(text: str) -> str:
    parser = _ConfirmationTextParser()
    parser.feed(text or " ")
    parser.close()
    cleaned = " ".join(parser.parts)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def validate_confirmation_evidence(*, confirmation_url: str, confirmation_text: str) -> str:
    normalized_text = normalize_confirmation_text(confirmation_text)
    evidence = " ".join((confirmation_url.strip(), normalized_text)).strip()
    if not evidence:
        raise ValueError("Verified confirmation evidence is required before recording submission")

    lowered = evidence.casefold()
    success_markers = (
        "application received",
        "application has been received",
        "application submitted",
        "thank you for applying",
        "submission confirmed",
        "received your application",
    )
    if not any(marker in lowered for marker in success_markers):
        raise ValueError("Verified confirmation evidence is required before recording submission")
    return normalized_text


def submission_row(job: dict, *, confirmation_url: str, confirmation_text: str) -> list[str]:
    normalized_text = validate_confirmation_evidence(
        confirmation_url=confirmation_url,
        confirmation_text=confirmation_text,
    )
    notes = f"Confirmation verified: {normalized_text or confirmation_url.strip()}"
    if confirmation_url.strip() and normalized_text:
        notes += f" ({confirmation_url.strip()})"
    return [
        job["company"].strip(),
        "Submitted - Pending Response",
        job["role"].strip(),
        (job.get("salary") or "").strip(),
        date.today().isoformat(),
        job["url"].strip(),
        "N/A",
        notes,
    ]


def validate_profile(profile: dict) -> list[str]:
    errors: list[str] = []
    for path in (("name", "full"), ("contact", "email"), ("contact", "phone"), ("resume", "primary")):
        value = profile
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if not value:
            errors.append(".".join(path))
    resume = Path(profile.get("resume", {}).get("primary", "")).expanduser()
    if str(resume) and not resume.is_file():
        errors.append("resume.primary:file_missing")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scan", help="scanner.py JSON output")
    parser.add_argument("--profile", default=str(BASE / "profile.json"))
    parser.add_argument("--output", default=str(BASE / "pipeline-plan.json"))
    args = parser.parse_args()

    profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    errors = validate_profile(profile)
    if errors:
        raise SystemExit("Invalid profile: " + ", ".join(errors))
    scan = json.loads(Path(args.scan).read_text(encoding="utf-8"))
    routed = route_candidates(scan)
    payload = {
        "mode": "plan_only",
        "submission_enabled": False,
        "routes": routed,
        "counts": {key: len(value) for key, value in routed.items()},
    }
    Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
