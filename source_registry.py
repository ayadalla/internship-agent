"""Versioned allowlist for public Greenhouse and Lever source boards."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SUPPORTED_PLATFORMS = frozenset({"greenhouse", "lever"})


def load_registry(path: str | Path) -> dict[str, Any]:
    """Load active approved sources in a deterministic platform/token order."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Unsupported source registry version")
    entries = payload.get("sources")
    if not isinstance(entries, list):
        raise ValueError("Source registry sources must be a list")

    sources: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Source registry entry must be an object")
        platform = entry.get("platform")
        token = entry.get("token")
        if platform not in SUPPORTED_PLATFORMS or not isinstance(token, str) or not token.strip():
            raise ValueError("Source registry entry has unsupported platform or empty token")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", token):
            raise ValueError("Source registry token must be an exact board identifier, not a URL or path")
        identity = (platform, token)
        if entry.get("approved") is True and identity not in seen:
            sources.append({"platform": platform, "token": token})
            seen.add(identity)
    return {"version": 1, "sources": sorted(sources, key=lambda item: (item["platform"], item["token"]))}
