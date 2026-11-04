"""Durable page-action recovery decisions that never replay uncertain submission."""
from __future__ import annotations

import json
from pathlib import Path


def record_page_action(path: Path, *, action: str, evidence: dict[str, object]) -> None:
    """Append one sanitized page action/evidence pair for later recovery."""
    if action not in {"click", "upload", "save", "submit"}:
        raise ValueError("unsupported page action")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"action": action, "evidence": evidence}, sort_keys=True) + "\n")


def resume_from_verified_page_state(path: Path) -> dict[str, str]:
    """Resume only from verified state; an uncertain submit is inspection-only."""
    entries = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]
    if not entries:
        return {"status": "blocked", "blocker": "no page recovery evidence", "next_action": "inspect_page"}
    latest = entries[-1]
    action = latest.get("action")
    evidence = latest.get("evidence", {})
    if action == "submit" and isinstance(evidence, dict) and evidence.get("verified") is not True:
        return {
            "status": "blocked",
            "blocker": "uncertain submit requires confirmation inspection",
            "next_action": "inspect_confirmation_without_replay",
        }
    if isinstance(evidence, dict) and evidence.get("verified") is True:
        return {"status": "resumable", "next_action": "inspect_page"}
    return {"status": "blocked", "blocker": "page action was not verified", "next_action": "inspect_page"}
