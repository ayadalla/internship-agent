from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _manifest(tmp_path: Path, *, mode: str = "sanitized_local") -> dict:
    return {
        "schema_version": 1,
        "mode": mode,
        "job_id": 41,
        "queue_id": "queue-41",
        "target": {
            "id": "target-abc",
            "url": "https://job-boards.greenhouse.io/fixture/REQ-123",
        },
        "identity": {
            "company": "Sanitized Example",
            "role": "Software Engineer Intern",
            "requisition": "REQ-123",
            "platform": "greenhouse",
            "tenant": "fixture",
        },
        "profile": {
            "path": str(tmp_path / "profile.json"),
            "sha256": "1" * 64,
            "verified": True,
        },
        "resume": {
            "path": str(tmp_path / "Resume.pdf"),
            "basename": "Resume.pdf",
            "content_type": "application/pdf",
            "sha256": "2" * 64,
            "verified": True,
        },
        "manual_gate": {
            "gates": [],
            "maango": False,
            "maango_approved": False,
            "verified": True,
        },
        "runtime_paths": {
            "preparation": str(tmp_path / "preparation.json"),
            "review": str(tmp_path / "review.json"),
            "authorization_db": str(tmp_path / "authorization.sqlite3"),
            "authorization_handoff": str(tmp_path / "authorization.handoff"),
            "submit_journal": str(tmp_path / "submit.jsonl"),
            "confirmation": str(tmp_path / "confirmation.json"),
            "transaction_db": str(tmp_path / "transactions.sqlite3"),
            "status": str(tmp_path / "status.json"),
        },
    }


def test_manifest_loads_only_closed_versioned_exact_identity_contract(tmp_path):
    import live_run_manifest

    payload = _manifest(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = live_run_manifest.load_manifest(
        path,
        observed_binding={
            "target_id": "target-abc",
            "page_url": "https://job-boards.greenhouse.io/fixture/REQ-123",
            "company": "Sanitized Example",
            "role": "Software Engineer Intern",
            "requisition": "REQ-123",
            "platform": "greenhouse",
            "tenant": "fixture",
        },
    )

    assert loaded == payload


def test_manifest_accepts_profile_selected_pdf_basename_instead_of_forcing_generic_name(
    tmp_path,
):
    import live_run_manifest

    payload = _manifest(tmp_path, mode="production_live")
    payload["resume"]["path"] = str(tmp_path / "Resume 2027 SWE.pdf")
    payload["resume"]["basename"] = "Resume 2027 SWE.pdf"

    assert live_run_manifest.validate_manifest(
        payload, production_enabled=True
    ) == payload


def test_manifest_rejects_unknown_fields_production_without_enablement_and_identity_drift(
    tmp_path,
):
    import live_run_manifest

    payload = _manifest(tmp_path)
    payload["unexpected"] = "unsafe"
    with pytest.raises(live_run_manifest.ManifestError, match="unknown manifest fields"):
        live_run_manifest.validate_manifest(payload)

    production = _manifest(tmp_path, mode="production_live")
    with pytest.raises(live_run_manifest.ManifestError, match="explicitly enabled"):
        live_run_manifest.validate_manifest(production)
    assert live_run_manifest.validate_manifest(
        production, production_enabled=True
    ) == production

    sanitized = _manifest(tmp_path)
    with pytest.raises(live_run_manifest.ManifestError, match="identity drift"):
        live_run_manifest.validate_manifest(
            sanitized,
            observed_binding={
                "target_id": "target-other",
                "page_url": sanitized["target"]["url"],
                "company": sanitized["identity"]["company"],
                "role": sanitized["identity"]["role"],
                "requisition": sanitized["identity"]["requisition"],
                "platform": sanitized["identity"]["platform"],
                "tenant": sanitized["identity"]["tenant"],
            },
        )
