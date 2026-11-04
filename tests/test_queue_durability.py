"""Offline regressions for durable autonomous queue boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app_queue import ApplicationQueue

NOW = "2026-09-05T18:00:00+00:00"


def at(seconds):
    return (datetime.fromisoformat(NOW) + timedelta(seconds=seconds)).isoformat()


def enqueue(queue, number=1, platform="Greenhouse"):
    return queue.enqueue(company="Example", role="Intern", ats_platform=platform,
                         url=f"https://job-boards.greenhouse.io/example/jobs/{number}")


def test_concurrent_claimers_cannot_both_receive_one_job(tmp_path):
    # Force the original SELECT-before-BEGIN interleaving on real SQLite.
    barrier = threading.Barrier(2)

    class InterleavedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if "SELECT" in sql and "LIMIT 1" in sql and not self.in_transaction:
                barrier.wait(timeout=5)
            return result

    class InterleavedQueue(ApplicationQueue):
        def _connect(self):
            return sqlite3.connect(self.path, factory=InterleavedConnection)

    queue = InterleavedQueue(tmp_path / "queue.db")
    enqueue(queue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: queue.lease_next(now=NOW, lease_seconds=60), range(2)))
    assert sum(job is not None for job in results) == 1
    assert queue.list_jobs()[0].attempt_count == 1


def test_replaced_owner_cannot_finish_new_lease(tmp_path):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    first = queue.lease_next(now=NOW, lease_seconds=60)
    replacement = queue.lease_next(now=at(60), lease_seconds=60)
    assert getattr(first, "lease_token", None), "claims need an opaque lease token"
    assert first.lease_token != replacement.lease_token
    with pytest.raises(ValueError, match="lease"):
        queue.finish_lease(first.id, lease_token=first.lease_token, outcome="prepared", now=at(61))
    assert queue.list_jobs()[0] == replacement
    finished = queue.finish_lease(replacement.id, lease_token=replacement.lease_token,
                                  outcome="prepared", now=at(61))
    assert finished.state == "prepared"
    assert finished.lease_token is None


def test_heartbeat_renews_only_current_unexpired_owner(tmp_path):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    first = queue.lease_next(now=NOW, lease_seconds=60)
    assert callable(getattr(queue, "heartbeat", None)), "a worker must be able to renew its lease"
    renewed = queue.heartbeat(first.id, lease_token=first.lease_token, now=at(30), lease_seconds=90)
    assert renewed.lease_expires_at == at(120)
    assert renewed.attempt_count == first.attempt_count
    assert renewed.lease_token == first.lease_token
    assert queue.lease_next(now=at(60), lease_seconds=60) is None
    with pytest.raises(ValueError, match="lease"):
        queue.heartbeat(first.id, lease_token="wrong", now=at(31), lease_seconds=900)
    with pytest.raises(ValueError, match="lease"):
        queue.heartbeat(first.id, lease_token=first.lease_token, now=at(120), lease_seconds=900)
    with pytest.raises(ValueError, match="lease"):
        queue.finish_lease(first.id, lease_token=first.lease_token, now=at(120), outcome="prepared")
    assert queue.list_jobs()[0] == renewed


def test_transition_cannot_bypass_lease_ownership(tmp_path):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    leased = queue.lease_next(now=NOW, lease_seconds=60)
    with pytest.raises(ValueError, match="lease"):
        queue.transition(leased.id, "prepared")
    assert queue.list_jobs()[0] == leased


@pytest.mark.parametrize("outcome", ["blocked_security", "blocked_fact", "blocked_approval", "submission_uncertain"])
def test_parked_candidates_are_inspectable_but_never_leased(tmp_path, outcome):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    leased = queue.lease_next(now=NOW, lease_seconds=60)
    parked = queue.finish_lease(leased.id, lease_token=leased.lease_token, outcome=outcome, now=at(1))
    assert parked.state == outcome
    assert queue.lease_next(now=at(600), lease_seconds=60) is None
    assert queue.inspect_candidates() == [parked]
    assert queue.inspect_candidates(states=(outcome,)) == [parked]
    assert queue.list_jobs() == [parked]
    if outcome == "submission_uncertain":
        with pytest.raises(ValueError, match="transition"):
            queue.transition(parked.id, "discovered")


def test_legacy_sqlite_migration_preserves_work_and_parks_unfenced_leases(tmp_path):
    path = tmp_path / "queue.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE application_queue (
            id INTEGER PRIMARY KEY, company TEXT, role TEXT, normalized_url TEXT UNIQUE,
            ats_platform TEXT, state TEXT)""")
        for number, state in enumerate(("discovered", "pending_question", "leased"), 1):
            conn.execute("INSERT INTO application_queue VALUES (?, 'E', 'I', ?, 'Greenhouse', ?)",
                         (number, f"https://example.com/{number}", state))
    queue = ApplicationQueue(path)
    assert [job.state for job in queue.list_jobs()] == ["discovered", "pending_question", "submission_uncertain"]
    assert len(queue.inspect_candidates()) == 2
    assert ApplicationQueue(path).list_jobs() == queue.list_jobs()
    assert queue.lease_next(now=NOW, lease_seconds=60).lease_token


def test_journal_migrates_valid_jsonl_to_sqlite_despite_torn_last_line(tmp_path):
    import json
    from execution_journal import ExecutionJournal
    path = tmp_path / "journal.jsonl"
    entry = dict(job_id=1, attempt_count=1, step="prepared_plan_written", payload={"plan_path": "one.json"})
    original = json.dumps(entry) + '\n{"job_id":2,"payload":'
    path.write_text(original, encoding="utf-8")
    journal = ExecutionJournal(path)
    assert journal.latest_step(job_id=1, attempt_count=1) == entry
    journal.append(job_id=3, attempt_count=1, step="lease_claimed", payload={})
    assert journal.latest_step(job_id=3, attempt_count=1)["step"] == "lease_claimed"
    assert path.read_text(encoding="utf-8") == original, "legacy evidence must be preserved"
    with sqlite3.connect(journal.db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT count(*) FROM execution_entries").fetchone() == (2,)
    assert ExecutionJournal(path).read_all() == journal.read_all()


def test_reclaimed_lease_recovers_durable_job_checkpoint_without_reprepare(tmp_path, monkeypatch):
    import queue_worker
    from execution_journal import ExecutionJournal
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    journal = ExecutionJournal(tmp_path / "journal.db")
    original_finish = queue.finish_lease
    monkeypatch.setattr(queue, "finish_lease", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    kwargs = dict(queue=queue, journal=journal, expected_resume_basename=None,
                  lease_seconds=60, plan_dir=tmp_path / "plans")
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse.html"
    with pytest.raises(RuntimeError, match="crash"):
        queue_worker.prepare_next_job(**kwargs, now=NOW, html_loader=lambda job: fixture.read_text(encoding="utf-8"))
    monkeypatch.setattr(queue, "finish_lease", original_finish)
    def forbidden_loader(job):
        raise AssertionError("checkpoint recovery must not reload or reprepare")
    result = queue_worker.prepare_next_job(**kwargs, now=at(61), html_loader=forbidden_loader)
    assert result["recovered"] is True
    assert result["queue_job"]["attempt_count"] == 2
    assert result["queue_job"]["state"] == "prepared"
    assert result["plan_path"].endswith("job-1-attempt-1.json")
    assert len(list((tmp_path / "plans").glob("*.json"))) == 1


def test_security_gate_is_parked_without_prepared_success_or_blocking_next_job(tmp_path, capsys):
    import json
    import queue_worker
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    enqueue(queue, 2)
    html = tmp_path / "gate.html"
    html.write_text('<html><form><input name="name"></form><div>Complete assessment</div></html>', encoding="utf-8")
    code = queue_worker.main([
        "--queue-db", str(queue.path), "--journal", str(tmp_path / "journal.db"),
        "--html-path", str(html), "--plan-dir", str(tmp_path / "plans"), "--now", NOW,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert queue.list_jobs()[0].state == "blocked_security"
    assert code == 2
    assert payload["status"] == "blocked_security"
    assert queue.lease_next(now=at(1), lease_seconds=60).id == 2


@pytest.mark.parametrize("payload, expected", [
    ({"safe_to_prepare": False}, "blocked_fact"),
    ({}, "blocked_fact"),
    ({"safe_to_prepare": True, "human_gate": "assessment"}, "blocked_security"),
    ({"safe_to_prepare": False, "manual_gate": {"type": "approval"}}, "blocked_approval"),
    ({"safe_to_prepare": True, "approval_required": True}, "blocked_approval"),
    ({"safe_to_prepare": False, "manual_gate": {"type": "missing_fact"}}, "blocked_fact"),
])
def test_unsafe_plan_never_becomes_prepared_on_initial_or_recovery_attempt(tmp_path, payload, expected):
    import json
    import queue_worker
    from execution_journal import ExecutionJournal
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    journal = ExecutionJournal(tmp_path / "journal.db")
    first = queue.lease_next(now=NOW, lease_seconds=60)
    artifact = tmp_path / "unsafe.json"
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    journal.append(job_id=first.id, attempt_count=1, step="prepared_plan_written",
                   payload={"plan_path": str(artifact)})
    replacement = queue.lease_next(now=at(60), lease_seconds=60)
    result = queue_worker.resume_or_prepare_leased_job(
        queue=queue, leased_job=replacement, journal=journal, html_text="unused",
        expected_resume_basename=None, now=at(61), plan_dir=tmp_path / "plans")
    assert result["queue_job"]["state"] == expected
    assert queue.lease_next(now=at(1000), lease_seconds=60) is None


def test_immediate_retries_have_bounded_backoff_and_do_not_starve_untouched_job(tmp_path):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    enqueue(queue, 2)
    first = queue.lease_next(now=NOW, lease_seconds=60)
    retry = queue.finish_lease(first.id, lease_token=first.lease_token, outcome="retry", now=at(1))
    assert datetime.fromisoformat(retry.available_at) > datetime.fromisoformat(at(1))
    # Both now eligible: the older retry must not outrank untouched work.
    second = queue.lease_next(now=at(1000), lease_seconds=60)
    assert second.id == 2
    bounded = queue.finish_lease(second.id, lease_token=second.lease_token, outcome="retry",
                                 now=at(1001), retry_seconds=10**20)
    assert 0 < (datetime.fromisoformat(bounded.available_at) - datetime.fromisoformat(at(1001))).total_seconds() <= 3600


@pytest.mark.parametrize("platform_failures, budget, expected", [(10, 3, "discovered"), (0, 1, "failed")])
def test_worker_retry_budget_is_per_job_not_platform_history(tmp_path, monkeypatch, capsys, platform_failures, budget, expected):
    import queue_worker
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    circuits = queue_worker.ATSCircuitBreaker(tmp_path / "circuits.db")
    for _ in range(platform_failures):
        circuits.record_failure(platform="Greenhouse", now=at(-1000), cooldown_seconds=1)
    def outage(**kwargs):
        raise ValueError("Browser capture unavailable")
    monkeypatch.setattr(queue_worker.prepare_job, "prepare_saved_html", outage)
    arguments = ["--queue-db", str(queue.path), "--journal", str(tmp_path / "journal.db"),
                 "--html-path", str(Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse.html"),
                 "--plan-dir", str(tmp_path / "plans"), "--now", NOW, "--ats-retry-budget", str(budget)]
    if platform_failures:
        arguments += ["--circuit-db", str(circuits.path)]
    assert queue_worker.main(arguments) == 2
    assert queue.list_jobs()[0].state == expected


def test_queue_enforces_durable_per_job_budget_for_controller_retries(tmp_path):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    for attempt in range(3):
        lease = queue.lease_next(now=at(attempt * 1000), lease_seconds=60)
        finished = queue.finish_lease(lease.id, lease_token=lease.lease_token, outcome="retry", now=at(attempt * 1000 + 1))
        queue = ApplicationQueue(queue.path)
    assert finished.state == "failed"
    assert queue.lease_next(now=at(10000), lease_seconds=60) is None


def test_platform_failure_history_decays_after_cooldown(tmp_path):
    from queue_worker import ATSCircuitBreaker
    circuits = ATSCircuitBreaker(tmp_path / "circuits.db")
    assert circuits.record_failure(platform="Greenhouse", now=NOW, cooldown_seconds=60) == 1
    assert circuits.record_failure(platform="Greenhouse", now=at(1), cooldown_seconds=60) == 2
    assert circuits.open_platforms(now=at(61)) == ()
    assert circuits.record_failure(platform="Greenhouse", now=at(61), cooldown_seconds=10**20) == 1
    assert circuits.open_platforms(now=at(3662)) == ()


def test_success_resets_platform_circuit_but_not_a_newer_failure(tmp_path):
    from queue_worker import ATSCircuitBreaker
    circuits = ATSCircuitBreaker(tmp_path / "circuits.db")
    circuits.record_failure(platform="Greenhouse", now=NOW, cooldown_seconds=60)
    assert callable(getattr(circuits, "record_success", None))
    circuits.record_success(platform="Greenhouse", now=at(1))
    assert circuits.open_platforms(now=at(2)) == ()
    assert circuits.record_failure(platform="Greenhouse", now=at(10), cooldown_seconds=60) == 1
    circuits.record_success(platform="Greenhouse", now=at(1))
    assert circuits.open_platforms(now=at(11)) == ("greenhouse",)


def test_gate_clearance_invalidates_checkpoint_even_if_finished_audit_was_lost(tmp_path, monkeypatch):
    import queue_worker
    from execution_journal import ExecutionJournal
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    journal = ExecutionJournal(tmp_path / "journal.db")
    original_append = journal.append
    def crash_after_queue_finish(**kwargs):
        if kwargs["step"] == "lease_finished":
            raise RuntimeError("audit crash")
        return original_append(**kwargs)
    monkeypatch.setattr(journal, "append", crash_after_queue_finish)
    kwargs = dict(queue=queue, journal=journal, expected_resume_basename=None,
                  lease_seconds=60, plan_dir=tmp_path / "plans")
    with pytest.raises(RuntimeError, match="audit crash"):
        queue_worker.prepare_next_job(**kwargs, now=NOW,
            html_loader=lambda job: '<form><input name="name"></form>assessment')
    assert queue.list_jobs()[0].state == "blocked_security"
    # The controller has independently verified manual gate clearance.
    queue.transition(1, "discovered")
    monkeypatch.setattr(journal, "append", original_append)
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse.html"
    result = queue_worker.prepare_next_job(**kwargs, now=at(1), html_loader=lambda job: fixture.read_text(encoding="utf-8"))
    assert result["recovered"] is False
    assert result["queue_job"]["state"] == "prepared"


def test_lease_expiry_compares_instants_not_timezone_strings(tmp_path):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    first = queue.lease_next(now="2026-09-05T11:00:00-07:00", lease_seconds=60)
    assert queue.lease_next(now=at(59), lease_seconds=60) is None
    assert queue.lease_next(now=at(60), lease_seconds=60).id == first.id


def test_replaced_worker_cannot_write_checkpoint_before_rejected_finish(tmp_path):
    import queue_worker
    from execution_journal import ExecutionJournal
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    first = queue.lease_next(now=NOW, lease_seconds=60)
    replacement = queue.lease_next(now=at(60), lease_seconds=60)
    journal = ExecutionJournal(tmp_path / "journal.db")
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse.html"
    with pytest.raises(ValueError, match="lease"):
        queue_worker.resume_or_prepare_leased_job(
            queue=queue, leased_job=first, journal=journal, html_text=fixture.read_text(encoding="utf-8"),
            expected_resume_basename=None, now=at(61), plan_dir=tmp_path / "plans")
    assert journal.read_all() == []
    assert not list((tmp_path / "plans").glob("*.json"))
    assert queue.list_jobs()[0] == replacement


def test_plan_checkpoint_is_published_atomically_before_journal_commit(tmp_path, monkeypatch):
    import os
    import queue_worker
    from execution_journal import ExecutionJournal
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    journal = ExecutionJournal(tmp_path / "journal.db")
    def interrupted_replace(*args, **kwargs):
        raise OSError("atomic publish interrupted")
    monkeypatch.setattr(os, "replace", interrupted_replace)
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse.html"
    with pytest.raises(OSError, match="atomic publish interrupted"):
        queue_worker.prepare_next_job(queue=queue, journal=journal, now=NOW, lease_seconds=60,
            html_loader=lambda job: fixture.read_text(encoding="utf-8"), expected_resume_basename=None, plan_dir=tmp_path / "plans")
    assert [entry["step"] for entry in journal.read_all()] == ["lease_claimed"]
    assert not list((tmp_path / "plans").iterdir())
    assert queue.list_jobs()[0].state == "leased"


@pytest.mark.parametrize("state", ["pending_question", "pending_approval"])
def test_legacy_human_gates_cannot_skip_reinspection_to_prepared(tmp_path, state):
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    lease = queue.lease_next(now=NOW, lease_seconds=60)
    parked = queue.finish_lease(lease.id, lease_token=lease.lease_token, outcome=state, now=at(1))
    with pytest.raises(ValueError, match="transition"):
        queue.transition(lease.id, "prepared")
    assert queue.list_jobs()[0] == parked


def test_mid_prepare_replacement_is_reported_without_poisoning_circuit(tmp_path, monkeypatch, capsys):
    import json
    import queue_worker
    from execution_journal import ExecutionJournal
    queue = ApplicationQueue(tmp_path / "queue.db")
    enqueue(queue)
    prepare = queue_worker.prepare_job.prepare_saved_html
    def expire_during_prepare(**kwargs):
        queue.lease_next(now=at(300), lease_seconds=600)
        return prepare(**kwargs)
    monkeypatch.setattr(queue_worker.prepare_job, "prepare_saved_html", expire_during_prepare)
    code = queue_worker.main([
        "--queue-db", str(queue.path), "--journal", str(tmp_path / "journal.db"),
        "--html-path", str(Path(__file__).resolve().parents[1] / "fixtures" / "greenhouse.html"),
        "--plan-dir", str(tmp_path / "plans"), "--circuit-db", str(tmp_path / "circuits.db"), "--now", NOW])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["status"] == "lease_lost"
    assert queue.list_jobs()[0].attempt_count == 2
    assert queue.list_jobs()[0].state == "leased"
    assert queue_worker.ATSCircuitBreaker(tmp_path / "circuits.db").open_platforms(now=NOW) == ()
    assert [e["step"] for e in ExecutionJournal(tmp_path / "journal.db").read_all()] == ["lease_claimed"]
