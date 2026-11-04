#!/usr/bin/env python3
"""Offline setup diagnostics for the Hermes job agent."""
from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path
from typing import Callable

import browser_health

BASE = Path(__file__).resolve().parent
DEFAULT_OAUTH_TOKEN = Path.home() / ".hermes/google_token.json"

BrowserProbe = Callable[[str], dict]


def _profile_check(profile_path: Path) -> dict:
    if not profile_path.exists():
        return {
            "status": "blocking",
            "error_code": "missing_profile",
            "message": f"Profile not found: {profile_path}",
        }

    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {
            "status": "blocking",
            "error_code": "invalid_profile",
            "message": f"Cannot read profile JSON: {profile_path} ({exc})",
        }

    if not isinstance(profile, dict) or any(
        not isinstance(profile.get(section, {}), dict) for section in ("resume", "contact")
    ):
        return {
            "status": "blocking",
            "error_code": "invalid_profile",
            "message": "Profile must be a JSON object with resume and contact objects",
        }

    resume_value = profile.get("resume", {}).get("primary")
    if not isinstance(resume_value, str) or not resume_value.strip():
        return {
            "status": "blocking",
            "error_code": "missing_resume_path",
            "message": "Profile must provide a nonempty resume.primary path",
        }
    resume_path = Path(resume_value).expanduser()
    try:
        resume_stat = resume_path.stat()
    except (OSError, ValueError) as exc:
        return {
            "status": "blocking",
            "error_code": "missing_resume_file" if isinstance(exc, (FileNotFoundError, NotADirectoryError))
                          else "unreadable_resume_file",
            "message": f"Cannot inspect resume file: {resume_path} ({exc})",
        }
    if not stat.S_ISREG(resume_stat.st_mode):
        return {
            "status": "blocking",
            "error_code": "missing_resume_file",
            "message": f"Resume path is not an existing file: {resume_path}",
        }

    if resume_stat.st_size == 0:
        return {
            "status": "blocking",
            "error_code": "empty_resume_file",
            "message": f"Resume file is empty: {resume_path}",
        }

    email = profile.get("contact", {}).get("email")
    if not isinstance(email, str) or not email.strip():
        return {
            "status": "blocking",
            "error_code": "missing_contact_email",
            "message": "Profile is missing contact.email",
        }

    return {
        "status": "ready",
        "error_code": None,
        "message": "Profile, contact email, and resume file are present",
        "resume_path": str(resume_path),
    }


def _oauth_check(oauth_token_path: Path) -> dict:
    if oauth_token_path.exists():
        return {
            "status": "ready",
            "error_code": None,
            "message": f"OAuth token present: {oauth_token_path}; API access not verified",
        }
    return {
        "status": "blocking",
        "error_code": "missing_google_oauth_token",
        "message": f"Google Sheets write token not found: {oauth_token_path}",
    }


def run_checks(
    profile_path: Path,
    *,
    browser_base_url: str = "http://127.0.0.1:9222",
    probe_browser: BrowserProbe = browser_health.probe_cdp_health,
    oauth_token_path: Path | None = None,
    require_sheets: bool = False,
    skip_browser: bool = False,
) -> dict:
    profile = _profile_check(Path(profile_path))
    if require_sheets or oauth_token_path is not None:
        token_path = DEFAULT_OAUTH_TOKEN if oauth_token_path is None else Path(oauth_token_path)
        oauth = _oauth_check(token_path)
    else:
        oauth = {
            "status": "skipped",
            "error_code": "sheets_not_required",
            "message": "Google Sheets is optional; use --require-sheets to check its token",
        }

    if skip_browser:
        browser = {
            "status": "skipped",
            "error_code": "skipped_by_flag",
            "message": "Skipped browser check by request",
        }
    elif profile["status"] != "ready":
        browser = {
            "status": "skipped",
            "error_code": "profile_blocking_issue",
            "message": "Skipped browser check because profile prerequisites failed",
        }
    else:
        browser = probe_browser(browser_base_url)

    checks = {
        "profile": profile,
        "oauth": oauth,
        "browser": browser,
    }
    ready = all(check["status"] in {"ready", "skipped"} for check in checks.values())
    return {
        "status": "ready" if ready else "blocking",
        "ready": ready,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default=str(BASE / "profile.json"))
    parser.add_argument("--browser-base-url", default="http://127.0.0.1:9222")
    parser.add_argument("--oauth-token", help="Check an explicit Google Sheets token path")
    parser.add_argument("--require-sheets", action="store_true",
                        help="Require the optional Google Sheets OAuth token presence check")
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args(argv)

    payload = run_checks(
        Path(args.profile),
        browser_base_url=args.browser_base_url,
        oauth_token_path=Path(args.oauth_token) if args.oauth_token is not None else None,
        require_sheets=args.require_sheets,
        skip_browser=args.skip_browser,
    )
    print(json.dumps(payload, indent=2))
    return 0 if payload["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
