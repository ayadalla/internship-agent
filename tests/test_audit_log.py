from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import audit_log


def test_log_event_writes_jsonl_and_redacts_sensitive_values(tmp_path):
    log_path = tmp_path / "audit.jsonl"
    logger = audit_log.AuditLogger(log_path)

    logger.log(
        "scan.started",
        {
            "company": "Example",
            "contact": {
                "email": "kevin@example.com",
                "phone": "571-435-5734",
            },
            "resume_path": "/secret/resume.pdf",
            "url": "https://example.com/job/1",
        },
    )

    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert len(entries) == 1
    assert entries[0]["event"] == "scan.started"
    assert entries[0]["payload"] == {
        "company": "Example",
        "contact": {
            "email": "[REDACTED]",
            "phone": "[REDACTED]",
        },
        "resume_path": "[REDACTED]",
        "url": "https://example.com/job/1",
    }
