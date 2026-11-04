from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orchestrator
import production_run
import sources


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    """Fixtures are dated 2026-08-23; pin 'now' so the 30-day stale check does not drift."""
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 9, 1, tzinfo=timezone.utc))


def _write_profile(tmp_path: Path) -> Path:
    profile_path = tmp_path / "profile.json"
    resume_path = tmp_path / "resume.pdf"
    resume_path.write_text("fixture resume", encoding="utf-8")
    profile_path.write_text(
        json.dumps(
            {
                "name": {"full": "Test User"},
                "contact": {"email": "test@example.com", "phone": "555-1111"},
                "resume": {"primary": str(resume_path)},
                "preferences": {
                    "target_roles": ["Software Engineer Intern"],
                    "target_timelines": ["Summer 2027"],
                    "location_preference": "Remote or anywhere in U.S.",
                },
            }
        )
    , encoding="utf-8")
    return profile_path



def test_main_runs_sources_then_orchestrator_and_persists_all_artifacts(tmp_path, monkeypatch, capsys):
    profile_path = _write_profile(tmp_path)

    monkeypatch.setattr(orchestrator.scanner, "tracker_duplicate", lambda company, role, url: False)
    monkeypatch.setattr(
        sources,
        "fetch_greenhouse_jobs",
        lambda token, **kwargs: [
            {
                "company": "Example",
                "role": "Software Engineer Intern, Summer 2027",
                "url": "https://job-boards.greenhouse.io/example/jobs/123?utm_source=linkedin",
                "location": "Remote - United States",
                "source": "Greenhouse public API",
                "updated_at": "2026-08-23T00:00:00Z",
            }
        ],
    )

    workspace = tmp_path / "runtime"
    exit_code = production_run.main(
        [
            "--greenhouse",
            "example",
            "--profile",
            str(profile_path),
            "--workspace",
            str(workspace),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "production_safe_dry_run"
    assert payload["source_exit_code"] == 0
    assert payload["orchestrator_exit_code"] == 0

    candidates = json.loads((workspace / "candidates.json").read_text(encoding="utf-8"))
    assert candidates == [
        {
            "company": "Example",
            "role": "Software Engineer Intern, Summer 2027",
            "url": "https://job-boards.greenhouse.io/example/jobs/123",
            "location": "Remote - United States",
            "source": "Greenhouse public API",
            "updated_at": "2026-08-23T00:00:00Z",
        }
    ]

    source_report = json.loads((workspace / "sources-report.json").read_text(encoding="utf-8"))
    assert source_report["source_health_status"] == "healthy"

    orchestrator_report = json.loads((workspace / "orchestrator-report.json").read_text(encoding="utf-8"))
    assert orchestrator_report["mode"] == "dry_run"
    assert orchestrator_report["queue"]["count"] == 1


def test_main_proves_idempotent_dry_run_and_keeps_unsupported_roles_out_of_queue(tmp_path, monkeypatch, capsys):
    profile_path = _write_profile(tmp_path)

    monkeypatch.setattr(orchestrator.scanner, "tracker_duplicate", lambda company, role, url: False)
    monkeypatch.setattr(
        sources,
        "fetch_greenhouse_jobs",
        lambda token, **kwargs: [
            {
                "company": "Example",
                "role": "Software Engineer Intern, Summer 2027",
                "url": "https://job-boards.greenhouse.io/example/jobs/123?utm_source=linkedin",
                "location": "Remote - United States",
                "source": "Greenhouse public API",
                "updated_at": "2026-08-23T00:00:00Z",
            },
            {
                "company": "Example",
                "role": "Business Analyst Intern, Summer 2027",
                "url": "https://careers.example.com/jobs/456?utm_source=linkedin",
                "location": "Remote - United States",
                "source": "Greenhouse public API",
                "updated_at": "2026-08-23T00:00:00Z",
            },
        ],
    )

    workspace = tmp_path / "runtime"
    exit_code = production_run.main(
        [
            "--greenhouse",
            "example",
            "--profile",
            str(profile_path),
            "--workspace",
            str(workspace),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verification"] == {
        "idempotent_queueing": True,
        "first_queue_count": 1,
        "second_queue_count": 1,
        "unsupported_roles_not_queued": True,
        "submission_enabled": False,
        "external_side_effects_blocked": True,
    }

    queue_db = workspace / "app_queue.sqlite3"
    jobs = production_run.orchestrator.ApplicationQueue(queue_db).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].url == "https://job-boards.greenhouse.io/example/jobs/123"

    audit_lines = (workspace / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(audit_lines) == 2


def test_production_dry_run_accepts_registry_without_touching_sheets(tmp_path, monkeypatch, capsys):
    profile_path = _write_profile(tmp_path)
    def tracker_forbidden(*args):
        raise AssertionError("Sheets access is forbidden by default")
    monkeypatch.setattr(orchestrator.scanner, "tracker_duplicate", tracker_forbidden)
    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", lambda *args, **kwargs: [])
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"version": 1, "sources": [
        {"platform": "greenhouse", "token": "example", "approved": True}]}), encoding="utf-8")
    assert production_run.main(["--registry", str(registry), "--profile", str(profile_path),
                                "--workspace", str(tmp_path / "run")]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["source"]["greenhouse_tokens"] == ["example"]
    assert "orchestrator" not in result
