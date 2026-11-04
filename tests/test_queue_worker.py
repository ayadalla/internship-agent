from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_queue
import execution_journal
import prepare_job
import queue_worker


def test_prepare_next_job_skips_platform_with_open_circuit_and_leases_other_ats(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Broken Greenhouse",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/broken/jobs/123",
        ats_platform="Greenhouse",
    )
    queue.enqueue(
        company="Healthy Lever",
        role="Software Engineer Intern",
        url="https://jobs.lever.co/healthy/123",
        ats_platform="Lever",
    )
    circuits = queue_worker.ATSCircuitBreaker(tmp_path / "circuits.db")
    circuits.record_failure(platform="Greenhouse", now="2026-08-25T08:00:00+00:00", cooldown_seconds=600)

    result = queue_worker.prepare_next_job(
        queue=queue,
        journal=execution_journal.ExecutionJournal(tmp_path / "journal.jsonl"),
        html_loader=lambda leased_job: (ROOT / "fixtures" / "lever_application.html").read_text(encoding="utf-8"),
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
        now="2026-08-25T08:01:00+00:00",
        lease_seconds=300,
        plan_dir=tmp_path / "plans",
        circuit_breaker=circuits,
    )

    assert result is not None
    assert result["queue_job"]["ats_platform"] == "Lever"
    assert [job.state for job in queue.list_jobs()] == ["discovered", "prepared"]


def test_prepare_next_job_leases_discovered_job_writes_plan_and_finishes_prepared(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queued_job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    journal = execution_journal.ExecutionJournal(tmp_path / "journal.jsonl")
    plan_dir = tmp_path / "plans"

    result = queue_worker.prepare_next_job(
        queue=queue,
        journal=journal,
        html_loader=lambda leased_job: (ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8"),
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
        now="2026-08-25T07:35:00+00:00",
        lease_seconds=300,
        plan_dir=plan_dir,
    )

    expected_plan = prepare_job.prepare_saved_html(
        html_text=(ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8"),
        page_url=queued_job.url,
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )

    assert result is not None
    assert result["recovered"] is False
    assert result["plan_path"] == str(plan_dir / "job-1-attempt-1.json")
    assert json.loads(Path(result["plan_path"]).read_text(encoding="utf-8")) == expected_plan
    assert result["queue_job"]["state"] == "prepared"
    assert result["queue_job"]["attempt_count"] == 1

    entries = journal.read_all()
    assert [entry["step"] for entry in entries] == [
        "lease_claimed",
        "prepared_plan_written",
        "lease_finished",
    ]
    assert entries[0]["payload"]["lease_expires_at"] == "2026-08-25T07:40:00+00:00"


def test_prepare_next_job_retry_uses_attempt_specific_plan_path(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    first_lease = queue.lease_next(now="2026-08-25T07:35:00+00:00", lease_seconds=300)
    assert first_lease is not None
    queue.finish_lease(
        first_lease.id,
        lease_token=first_lease.lease_token,
        outcome="retry",
        now="2026-08-25T07:36:00+00:00",
        retry_seconds=60,
        error="temporary browser outage",
    )

    journal = execution_journal.ExecutionJournal(tmp_path / "journal.jsonl")
    plan_dir = tmp_path / "plans"

    result = queue_worker.prepare_next_job(
        queue=queue,
        journal=journal,
        html_loader=lambda leased_job: (ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8"),
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
        now="2026-08-25T07:37:30+00:00",
        lease_seconds=300,
        plan_dir=plan_dir,
    )

    assert result is not None
    assert result["plan_path"] == str(plan_dir / "job-1-attempt-2.json")
    assert result["queue_job"]["attempt_count"] == 2
    assert Path(result["plan_path"]).exists()


def test_resume_or_prepare_leased_job_recovers_after_plan_was_already_written(tmp_path, monkeypatch):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    job = queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T07:35:00+00:00", lease_seconds=300)
    assert leased is not None

    journal = execution_journal.ExecutionJournal(tmp_path / "journal.jsonl")
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    plan_path = plan_dir / "job-1-attempt-1.json"
    existing_plan = prepare_job.prepare_saved_html(
        html_text=(ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8"),
        page_url=job.url,
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
    )
    plan_path.write_text(json.dumps(existing_plan), encoding="utf-8")
    journal.append(
        job_id=leased.id,
        attempt_count=leased.attempt_count,
        step="prepared_plan_written",
        payload={"plan_path": str(plan_path)},
    )

    def fail_prepare(**kwargs):
        raise AssertionError("prepare_saved_html should not run during recovery")

    monkeypatch.setattr(queue_worker.prepare_job, "prepare_saved_html", fail_prepare)

    result = queue_worker.resume_or_prepare_leased_job(
        queue=queue,
        leased_job=leased,
        journal=journal,
        html_text="<html>should not be reparsed during recovery</html>",
        expected_resume_basename="Kevin_Pyo_Resume.pdf",
        now="2026-08-25T07:36:00+00:00",
        plan_dir=plan_dir,
    )

    assert result["recovered"] is True
    assert result["plan_path"] == str(plan_path)
    assert result["queue_job"]["state"] == "prepared"
    assert json.loads(plan_path.read_text(encoding="utf-8")) == existing_plan

    steps = [entry["step"] for entry in journal.read_all()]
    assert steps == ["prepared_plan_written", "lease_finished"]


def test_resume_or_prepare_leased_job_refuses_missing_recovered_plan_artifact(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T08:05:00+00:00", lease_seconds=300)
    assert leased is not None

    journal = execution_journal.ExecutionJournal(tmp_path / "journal.jsonl")
    missing_plan = tmp_path / "plans" / "job-1-attempt-1.json"
    journal.append(
        job_id=leased.id,
        attempt_count=leased.attempt_count,
        step="prepared_plan_written",
        payload={"plan_path": str(missing_plan)},
    )

    try:
        queue_worker.resume_or_prepare_leased_job(
            queue=queue,
            leased_job=leased,
            journal=journal,
            html_text="<html>must not reparse a missing recovered artifact</html>",
            expected_resume_basename="Kevin_Pyo_Resume.pdf",
            now="2026-08-25T08:06:00+00:00",
            plan_dir=tmp_path / "plans",
        )
    except ValueError as exc:
        assert str(exc) == f"Invalid recovered plan artifact: {missing_plan}"
    else:
        raise AssertionError("missing recovered plan artifact must block lease completion")

    [job] = queue.list_jobs()
    assert job.state == "leased"
    assert [entry["step"] for entry in journal.read_all()] == ["prepared_plan_written"]


def test_resume_or_prepare_leased_job_refuses_malformed_recovered_plan_artifact(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T08:05:00+00:00", lease_seconds=300)
    assert leased is not None

    journal = execution_journal.ExecutionJournal(tmp_path / "journal.jsonl")
    plan_path = tmp_path / "plans" / "job-1-attempt-1.json"
    plan_path.parent.mkdir()
    plan_path.write_text("{not valid json", encoding="utf-8")
    journal.append(
        job_id=leased.id,
        attempt_count=leased.attempt_count,
        step="prepared_plan_written",
        payload={"plan_path": str(plan_path)},
    )

    try:
        queue_worker.resume_or_prepare_leased_job(
            queue=queue,
            leased_job=leased,
            journal=journal,
            html_text="<html>must not reparse a malformed recovered artifact</html>",
            expected_resume_basename="Kevin_Pyo_Resume.pdf",
            now="2026-08-25T08:06:00+00:00",
            plan_dir=tmp_path / "plans",
        )
    except ValueError as exc:
        assert str(exc) == f"Invalid recovered plan artifact: {plan_path}"
    else:
        raise AssertionError("malformed recovered plan artifact must block lease completion")

    [job] = queue.list_jobs()
    assert job.state == "leased"
    assert [entry["step"] for entry in journal.read_all()] == ["prepared_plan_written"]


def test_main_leases_once_writes_machine_readable_result_and_stays_idempotent(tmp_path, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    journal_path = tmp_path / "journal.jsonl"
    plan_dir = tmp_path / "plans"

    first_exit_code = queue_worker.main([
        "--queue-db",
        str(tmp_path / "queue.db"),
        "--journal",
        str(journal_path),
        "--html-path",
        str(ROOT / "fixtures" / "greenhouse.html"),
        "--expected-resume-basename",
        "Kevin_Pyo_Resume.pdf",
        "--now",
        "2026-08-25T07:35:00+00:00",
        "--lease-seconds",
        "300",
        "--plan-dir",
        str(plan_dir),
    ])
    first_payload = json.loads(capsys.readouterr().out)

    second_exit_code = queue_worker.main([
        "--queue-db",
        str(tmp_path / "queue.db"),
        "--journal",
        str(journal_path),
        "--html-path",
        str(ROOT / "fixtures" / "greenhouse.html"),
        "--expected-resume-basename",
        "Kevin_Pyo_Resume.pdf",
        "--now",
        "2026-08-25T07:36:00+00:00",
        "--lease-seconds",
        "300",
        "--plan-dir",
        str(plan_dir),
    ])
    second_payload = json.loads(capsys.readouterr().out)

    assert first_exit_code == 0
    assert first_payload["status"] == "prepared"
    assert first_payload["result"]["plan_path"] == str(plan_dir / "job-1-attempt-1.json")
    assert Path(first_payload["result"]["plan_path"]).exists()
    assert first_payload["result"]["queue_job"]["state"] == "prepared"

    assert second_exit_code == 0
    assert second_payload == {"status": "no_job_available"}
    assert len(execution_journal.ExecutionJournal(journal_path).read_all()) == 3


def test_main_uses_profile_resume_preflight_in_persisted_plan(tmp_path, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example", role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123", ats_platform="Greenhouse",
    )
    resume = tmp_path / "Kevin_Pyo_Resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nresume")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"resume": {
        "primary": str(resume), "required_application_filename": resume.name,
        "do_not_use_for_applications": [],
    }}), encoding="utf-8")

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"), "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(ROOT / "fixtures" / "greenhouse.html"),
        "--profile", str(profile), "--now", "2026-08-25T07:35:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    plan = json.loads(Path(payload["result"]["plan_path"]).read_text(encoding="utf-8"))
    assert plan["resume_preflight"]["basename"] == "Kevin_Pyo_Resume.pdf"
    assert plan["resume_preflight"]["verified"] is True
    assert plan["resume_verified"] is True


def test_main_fails_closed_and_requeues_retryable_prepare_blocker(tmp_path, monkeypatch, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    journal_path = tmp_path / "journal.jsonl"
    plan_dir = tmp_path / "plans"

    def fail_prepare(**kwargs):
        raise ValueError("Browser capture unavailable")

    monkeypatch.setattr(queue_worker.prepare_job, "prepare_saved_html", fail_prepare)

    exit_code = queue_worker.main([
        "--queue-db",
        str(tmp_path / "queue.db"),
        "--journal",
        str(journal_path),
        "--html-path",
        str(ROOT / "fixtures" / "greenhouse.html"),
        "--now",
        "2026-08-25T07:35:00+00:00",
        "--lease-seconds",
        "300",
        "--plan-dir",
        str(plan_dir),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload == {
        "status": "prepare_blocked",
        "error": "Browser capture unavailable",
        "job_id": 1,
        "retryable": True,
        "submission_enabled": False,
    }

    [job] = queue.list_jobs()
    assert job.state == "discovered"
    assert job.attempt_count == 1
    assert job.last_error == "Browser capture unavailable"
    assert job.lease_expires_at is None

    entries = execution_journal.ExecutionJournal(journal_path).read_all()
    assert [entry["step"] for entry in entries] == [
        "lease_claimed",
        "prepare_blocked",
        "lease_finished",
    ]
    assert entries[1]["payload"] == {
        "error": "Browser capture unavailable",
        "retryable": True,
    }
    assert entries[2]["payload"]["state"] == "discovered"
    assert not list(plan_dir.glob("*.json"))


def test_main_skips_open_circuit_platform_and_prepares_another_ats_job(tmp_path, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Broken Greenhouse",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/broken/jobs/123",
        ats_platform="Greenhouse",
    )
    queue.enqueue(
        company="Healthy Lever",
        role="Software Engineer Intern",
        url="https://jobs.lever.co/healthy/123",
        ats_platform="Lever",
    )
    circuit_db = tmp_path / "circuits.db"
    queue_worker.ATSCircuitBreaker(circuit_db).record_failure(
        platform="Greenhouse",
        now="2026-08-25T08:00:00+00:00",
        cooldown_seconds=600,
    )

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(ROOT / "fixtures" / "lever_application.html"),
        "--now", "2026-08-25T08:01:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
        "--circuit-db", str(circuit_db),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "prepared"
    assert payload["result"]["queue_job"]["ats_platform"] == "Lever"
    assert [job.state for job in queue.list_jobs()] == ["discovered", "prepared"]


def test_main_terminally_fails_unsupported_ats_without_requeueing(tmp_path, monkeypatch, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://example.com/custom/apply",
        ats_platform="Custom",
    )
    journal_path = tmp_path / "journal.jsonl"

    monkeypatch.setattr(
        queue_worker.prepare_job,
        "prepare_saved_html",
        lambda **kwargs: (_ for _ in ()).throw(
            ValueError("Unsupported ATS for URL: https://example.com/custom/apply")
        ),
    )

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(journal_path),
        "--html-path", str(ROOT / "fixtures" / "greenhouse.html"),
        "--now", "2026-08-25T07:35:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
    ])
    payload = json.loads(capsys.readouterr().out)

    [job] = queue.list_jobs()
    assert exit_code == 2
    assert payload["status"] == "prepare_blocked"
    assert payload["retryable"] is False
    assert job.state == "failed"
    assert job.available_at is None
    assert [entry["step"] for entry in execution_journal.ExecutionJournal(journal_path).read_all()] == [
        "lease_claimed", "prepare_blocked", "lease_finished"
    ]


def test_main_terminally_closes_withdrawn_posting_before_preparation(tmp_path, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    closed_page = tmp_path / "withdrawn.html"
    closed_page.write_text("<html><body>This job is no longer available.</body></html>", encoding="utf-8")

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(closed_page),
        "--now", "2026-08-25T07:35:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
    ])
    payload = json.loads(capsys.readouterr().out)

    [job] = queue.list_jobs()
    assert exit_code == 2
    assert payload["status"] == "posting_closed"
    assert payload["retryable"] is False
    assert job.state == "failed"
    assert job.last_error == "Posting closed: This job is no longer available."


def test_main_terminally_closes_posting_that_is_no_longer_accepting_applications(tmp_path, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    closed_page = tmp_path / "closed.html"
    closed_page.write_text("<html><body>This job is no longer accepting applications.</body></html>", encoding="utf-8")

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(closed_page),
        "--now", "2026-08-25T07:35:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
    ])
    payload = json.loads(capsys.readouterr().out)

    [job] = queue.list_jobs()
    assert exit_code == 2
    assert payload["status"] == "posting_closed"
    assert payload["retryable"] is False
    assert job.state == "failed"
    assert job.last_error == "Posting closed: This job is no longer accepting applications."


def test_main_terminally_fails_corrupted_preparation_fixture(tmp_path, monkeypatch, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    monkeypatch.setattr(
        queue_worker.prepare_job,
        "prepare_saved_html",
        lambda **kwargs: (_ for _ in ()).throw(
            ValueError("Corrupted fixture: malformed HTML snapshot")
        ),
    )

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(ROOT / "fixtures" / "greenhouse.html"),
        "--now", "2026-08-25T07:35:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
    ])
    payload = json.loads(capsys.readouterr().out)

    [job] = queue.list_jobs()
    assert exit_code == 2
    assert payload["retryable"] is False
    assert job.state == "failed"
    assert job.last_error == "Corrupted fixture: malformed HTML snapshot"


def test_main_terminally_contains_platform_when_retry_budget_is_exhausted(tmp_path, monkeypatch, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Broken Greenhouse",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/broken/jobs/123",
        ats_platform="Greenhouse",
    )
    queue.enqueue(
        company="Healthy Lever",
        role="Software Engineer Intern",
        url="https://jobs.lever.co/healthy/123",
        ats_platform="Lever",
    )
    monkeypatch.setattr(
        queue_worker.prepare_job,
        "prepare_saved_html",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("Browser capture unavailable")),
    )

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(ROOT / "fixtures" / "greenhouse.html"),
        "--now", "2026-08-25T08:00:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
        "--circuit-db", str(tmp_path / "circuits.db"),
        "--ats-retry-budget", "1",
    ])
    payload = json.loads(capsys.readouterr().out)

    jobs = queue.list_jobs()
    assert exit_code == 2
    assert payload["retryable"] is False
    assert payload["retry_budget_exhausted"] is True
    assert jobs[0].state == "failed"
    assert jobs[0].last_error == "Browser capture unavailable"
    assert jobs[1].state == "discovered"


def test_main_records_retryable_preparation_failure_in_platform_circuit(tmp_path, monkeypatch, capsys):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    monkeypatch.setattr(
        queue_worker.prepare_job,
        "prepare_saved_html",
        lambda **kwargs: (_ for _ in ()).throw(ValueError("Browser capture unavailable")),
    )
    circuit_path = tmp_path / "circuits.db"

    exit_code = queue_worker.main([
        "--queue-db", str(tmp_path / "queue.db"),
        "--journal", str(tmp_path / "journal.jsonl"),
        "--html-path", str(ROOT / "fixtures" / "greenhouse.html"),
        "--now", "2026-08-25T07:35:00+00:00",
        "--plan-dir", str(tmp_path / "plans"),
        "--circuit-db", str(circuit_path),
        "--circuit-cooldown-seconds", "600",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["retryable"] is True
    assert queue_worker.ATSCircuitBreaker(circuit_path).open_platforms(
        now="2026-08-25T07:36:00+00:00"
    ) == ("greenhouse",)
