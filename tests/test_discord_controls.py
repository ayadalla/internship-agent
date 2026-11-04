from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_queue
from audit_log import AuditLogger
import discord_controls


def test_approve_control_requeues_matching_pending_approval_job(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-24T17:20:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_approval",
        now="2026-08-24T17:21:00+00:00",
        error="awaiting Discord approval",
    )

    result = discord_controls.handle_control(
        queue,
        control_id=discord_controls.build_control_id("approve", job.id),
    )

    assert result["status"] == "approved"
    assert result["job"]["id"] == job.id
    assert result["job"]["state"] == "discovered"
    assert result["job"]["attempt_count"] == 1


def test_reject_control_marks_matching_pending_approval_job_failed(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-24T17:20:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_approval",
        now="2026-08-24T17:21:00+00:00",
        error="awaiting Discord approval",
    )

    result = discord_controls.handle_control(
        queue,
        control_id=discord_controls.build_control_id("reject", job.id),
    )

    assert result["status"] == "rejected"
    assert result["job"]["id"] == job.id
    assert result["job"]["state"] == "failed"


def test_retry_control_requeues_matching_pending_question_job(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-24T17:20:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_question",
        now="2026-08-24T17:21:00+00:00",
        error="need answer about start month",
    )

    result = discord_controls.handle_control(
        queue,
        control_id=discord_controls.build_control_id("retry", job.id),
    )

    assert result["status"] == "retried"
    assert result["job"]["id"] == job.id
    assert result["job"]["state"] == "discovered"


def test_skip_control_marks_matching_pending_question_job_failed(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-24T17:20:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_question",
        now="2026-08-24T17:21:00+00:00",
        error="answer needed",
    )

    result = discord_controls.handle_control(
        queue,
        control_id=discord_controls.build_control_id("skip", job.id),
    )

    assert result["status"] == "skipped"
    assert result["job"]["id"] == job.id
    assert result["job"]["state"] == "failed"


def test_handle_control_rejects_action_when_job_state_does_not_match(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )

    with pytest.raises(ValueError, match="Control approve is not valid for state discovered"):
        discord_controls.handle_control(
            queue,
            control_id=discord_controls.build_control_id("approve", job.id),
        )


def test_controls_for_job_returns_only_actions_valid_for_its_state(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-24T17:20:00+00:00", lease_seconds=300)
    blocked = queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_question",
        now="2026-08-24T17:21:00+00:00",
        error="need answer about start month",
    )

    controls = discord_controls.controls_for_job(blocked)

    assert controls == [
        {"action": "retry", "control_id": f"job:{job.id}:retry"},
        {"action": "skip", "control_id": f"job:{job.id}:skip"},
    ]


def test_main_emits_machine_readable_json_for_control_action(tmp_path, capsys):
    queue_db = tmp_path / "queue.db"
    queue = app_queue.ApplicationQueue(queue_db)
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-24T17:20:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_approval",
        now="2026-08-24T17:21:00+00:00",
        error="awaiting Discord approval",
    )

    exit_code = discord_controls.main(
        [
            discord_controls.build_control_id("approve", job.id),
            "--queue-db",
            str(queue_db),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "approved"
    assert payload["job"]["id"] == job.id
    assert payload["job"]["state"] == "discovered"


def test_handle_control_authorizes_actor_and_writes_audit_event(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T10:00:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_approval",
        now="2026-08-25T10:01:00+00:00",
        error="awaiting Discord approval",
    )
    audit_path = tmp_path / "audit.jsonl"

    result = discord_controls.handle_control(
        queue,
        control_id=discord_controls.build_control_id("approve", job.id),
        actor_id="628145910",
        allowed_actor_ids={"628145910"},
        audit_logger=AuditLogger(audit_path),
    )

    [entry] = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert result["status"] == "approved"
    assert entry["event"] == "discord_queue_control_applied"
    assert entry["payload"] == {
        "action": "approve",
        "actor_id": "628145910",
        "control_id": f"job:{job.id}:approve",
        "job_id": job.id,
        "status": "approved",
    }


def test_handle_control_denies_unauthorized_actor_without_transition_and_audits_denial(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T10:00:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_approval",
        now="2026-08-25T10:01:00+00:00",
        error="awaiting Discord approval",
    )
    audit_path = tmp_path / "audit.jsonl"

    with pytest.raises(PermissionError, match="not authorized"):
        discord_controls.handle_control(
            queue,
            control_id=discord_controls.build_control_id("approve", job.id),
            actor_id="untrusted-user",
            allowed_actor_ids={"628145910"},
            audit_logger=AuditLogger(audit_path),
        )

    assert queue.list_jobs()[0].state == "pending_approval"
    [entry] = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert entry["event"] == "discord_queue_control_denied"
    assert entry["payload"] == {
        "actor_id": "untrusted-user",
        "control_id": f"job:{job.id}:approve",
        "reason": "unauthorized_actor",
    }


def test_handle_control_accepts_single_use_token_once_and_rejects_replay(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://jobs.example.com/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T10:00:00+00:00", lease_seconds=300)
    queue.finish_lease(
        job.id,
        lease_token=leased.lease_token,
        outcome="pending_approval",
        now="2026-08-25T10:01:00+00:00",
        error="awaiting Discord approval",
    )
    audit_path = tmp_path / "audit.jsonl"
    token = discord_controls.issue_control_token(
        queue,
        control_id=discord_controls.build_control_id("approve", job.id),
        actor_id="628145910",
        expires_at="2026-08-25T11:00:00+00:00",
    )

    result = discord_controls.handle_control(
        queue,
        control_id=discord_controls.build_control_id("approve", job.id),
        actor_id="628145910",
        token=token,
        now="2026-08-25T10:02:00+00:00",
        audit_logger=AuditLogger(audit_path),
    )

    assert result["status"] == "approved"
    with pytest.raises(PermissionError, match="replayed"):
        discord_controls.handle_control(
            queue,
            control_id=discord_controls.build_control_id("approve", job.id),
            actor_id="628145910",
            token=token,
            now="2026-08-25T10:03:00+00:00",
            audit_logger=AuditLogger(audit_path),
        )
    assert queue.list_jobs()[0].state == "discovered"
    events = [json.loads(line)["event"] for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert events == ["discord_queue_control_applied", "discord_queue_control_denied"]
