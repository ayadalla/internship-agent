"""Validated, secret-free learned tenant metadata for preparation plans."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse


def load_for_page(path: Path, *, page_url: str, platform: str) -> dict | None:
    """Return the single validated tenant record matching this ATS page, if any."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid learned tenant metadata") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Invalid learned tenant metadata")
    records = payload.get("tenants")
    hostname = urlparse(page_url).hostname
    if not isinstance(records, list) or not hostname:
        raise ValueError("Invalid learned tenant metadata")

    matches = [record for record in records if isinstance(record, dict)
               and record.get("platform") == platform and record.get("hostname") == hostname]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Invalid learned tenant metadata")

    record = matches[0]
    tenant = record.get("tenant")
    session_reference = record.get("session_reference")
    if (
        not isinstance(tenant, str)
        or tenant != hostname.split(".", 1)[0]
        or not isinstance(record.get("authenticated"), bool)
        or not isinstance(session_reference, str)
        or not session_reference.startswith("runtime-only:")
    ):
        raise ValueError("Invalid learned tenant metadata")

    return {
        "tenant": tenant,
        "platform": platform,
        "authenticated": record["authenticated"],
        "session_reference": session_reference,
    }
