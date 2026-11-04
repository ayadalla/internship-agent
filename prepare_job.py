#!/usr/bin/env python3
"""Non-submitting ATS prepare command over saved HTML."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ats_registry import resolve_handler
from resume_preflight import preflight_profile_resume
from tenant_metadata import load_for_page


def prepare_saved_html(
    *,
    html_text: str,
    page_url: str,
    expected_resume_basename: str | None = None,
    tenant_metadata: dict | None = None,
) -> dict:
    handler = resolve_handler(page_url=page_url, html_text=html_text)
    payload = handler.inspect_html(
        html_text,
        page_url=page_url,
        expected_resume_basename=expected_resume_basename,
    )
    result = {
        "platform": handler.platform,
        "submission_enabled": False,
        "page_url": page_url,
        **payload,
    }
    if tenant_metadata and tenant_metadata.get("platform") == handler.platform:
        authenticated = tenant_metadata.get("authenticated") is True
        result["tenant_session"] = {
            "tenant": tenant_metadata.get("tenant"),
            "authenticated": authenticated,
            "reuse_authenticated_session": authenticated,
            "account_creation_required": not authenticated,
            "session_reference": tenant_metadata.get("session_reference"),
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html_path")
    parser.add_argument("--page-url", required=True)
    parser.add_argument("--expected-resume-basename")
    parser.add_argument("--profile")
    parser.add_argument("--tenant-metadata")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    try:
        resume_evidence = preflight_profile_resume(args.profile) if args.profile else None
        expected_resume_basename = (
            resume_evidence["basename"] if resume_evidence else args.expected_resume_basename
        )
        html_text = Path(args.html_path).read_text(encoding="utf-8")
        handler = resolve_handler(page_url=args.page_url, html_text=html_text)
        tenant = (
            load_for_page(
                Path(args.tenant_metadata),
                page_url=args.page_url,
                platform=handler.platform,
            )
            if args.tenant_metadata
            else None
        )
        payload = prepare_saved_html(
            html_text=html_text,
            page_url=args.page_url,
            expected_resume_basename=expected_resume_basename,
            tenant_metadata=tenant,
        )
        if resume_evidence:
            payload["resume_preflight"] = resume_evidence
            payload["resume_verified"] = True
    except ValueError as exc:
        print(json.dumps({
            "error": str(exc),
            "page_url": args.page_url,
            "submission_enabled": False,
        }))
        return 2
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload))
    return 0 if payload.get("safe_to_prepare", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
