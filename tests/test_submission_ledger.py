"""Offline invariants: only synthetic identities, temporary SQLite, injected UTC."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_submission_authorization import authoritative_review
from submission_authorization import SubmissionAuthorizationStore

def test_production_requires_canonical_shared_path_and_full_identity(tmp_path, monkeypatch):
    import submission_ledger

    canonical = tmp_path / "canonical.sqlite3"
    monkeypatch.setattr(submission_ledger, "CANONICAL_LEDGER_PATH", canonical, raising=False)
    with pytest.raises(ValueError, match="canonical"):
        SubmissionAuthorizationStore(tmp_path / "manifest.sqlite3", production=True)
    assert not (tmp_path / "manifest.sqlite3").exists()
    store = SubmissionAuthorizationStore(canonical, production=True)
    with pytest.raises(ValueError, match="identity"):
        issue(store)
    issued = issue(store, **IDENTITY)
    with pytest.raises(ValueError, match="identity"):
        consume(store, issued)
    consume(store, issued, **IDENTITY)
    with pytest.raises(ValueError, match="canonical"):
        submission_ledger.SubmissionLedger(tmp_path / "bypass.sqlite3", production=True)


NOW = "2026-09-05T12:00:00+00:00"
LATER = "2026-09-05T12:05:00+00:00"
IDENTITY = {"account_id": "synthetic-account", "tenant": "synthetic-tenant"}


def issue(store, *, job_id=17, requisition="REQ-123", at=NOW, until=LATER,
          target_id="page-42", page_url="https://sanitized.example.test/apply/REQ-123", **identity):
    review = authoritative_review()
    review.pop("review_evidence_sha256")
    review["binding"].update(requisition=requisition, target_id=target_id, page_url=page_url)
    review["review_evidence_sha256"] = hashlib.sha256(
        json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return store.issue(job_id=job_id, review_evidence=review, actor="synthetic-actor",
                       issued_at=at, expires_at=until, **identity)


def consume(store, issued, *, now=NOW, **identity):
    return store.consume(token=issued["token"], current_binding=issued["binding"],
                         actor="synthetic-actor", now=now, **identity)


def test_shared_immutable_identity_ignores_manifest_job_target_and_review_changes(tmp_path):
    shared = tmp_path / "global.sqlite3"
    first_store = SubmissionAuthorizationStore(tmp_path / "run-a.sqlite3", ledger_path=shared)
    second_store = SubmissionAuthorizationStore(tmp_path / "run-b.sqlite3", ledger_path=shared)
    first = issue(first_store, **IDENTITY)
    second = issue(second_store, job_id=999, target_id="other-tab",
                   page_url="https://sanitized.example.test/requisition/REQ-123?tracking=other", **IDENTITY)
    assert first["binding"]["review_evidence_sha256"] != second["binding"]["review_evidence_sha256"]
    consume(first_store, first, **IDENTITY)
    with pytest.raises(PermissionError, match="application"):
        consume(second_store, second, **IDENTITY)
    with pytest.raises(PermissionError, match="application"):
        issue(second_store, job_id=1000, **IDENTITY)


def test_consume_rejects_future_token_without_burning_it_and_normalizes_utc(tmp_path):
    store = SubmissionAuthorizationStore(tmp_path / "time.sqlite3")
    issued = issue(store, at="2026-09-05T05:00:00-07:00", until="2026-09-05T05:05:00-07:00")
    assert issued["issued_at"] == NOW
    with pytest.raises(PermissionError, match="not yet valid"):
        consume(store, issued, now="2026-09-05T11:59:59Z")
    consume(store, issued, now=NOW)


@pytest.mark.parametrize("bad_time", ["2026-09-05T12:00:00", "synthetic-private-time", None])
def test_timestamp_validation_is_explicit_aware_and_sanitized(tmp_path, bad_time):
    store = SubmissionAuthorizationStore(tmp_path / "time.sqlite3")
    with pytest.raises(ValueError, match="timezone-aware") as error:
        issue(store, at=bad_time)
    assert "synthetic-private-time" not in str(error.value)
    with pytest.raises(ValueError, match="timezone-aware"):
        store.ledger.status(now=bad_time)


@pytest.mark.parametrize("state", ["unknown", "confirmed"])
def test_import_historical_attempt_is_idempotent_preserves_original_time_and_identity(tmp_path, state):
    store = SubmissionAuthorizationStore(tmp_path / "shared.sqlite3")
    kwargs = dict(source_id="synthetic-legacy:record-1", evidence_sha256="a" * 64, trusted=True,
                  job_id=17, requisition="REQ-123", state=state, attempted_at="2026-09-05T04:59:00-07:00",
                  confirmed_at=NOW if state == "confirmed" else None, **IDENTITY)
    attempt = store.ledger.import_historical_attempt(**kwargs)
    assert store.ledger.import_historical_attempt(**kwargs) == attempt
    assert attempt["reserved_at"] == "2026-09-05T11:59:00+00:00"
    assert attempt["confirmed_at"] == (NOW if state == "confirmed" else None)
    assert store.ledger.status(now="2026-09-05T12:59:59Z")["eligible"] is False
    assert store.ledger.status(now="2026-09-05T13:00:00Z")["eligible"] is (state == "confirmed")
    with pytest.raises(PermissionError, match="application"):
        issue(store, job_id=999, **IDENTITY)
    if state == "unknown":
        # Preserve ALL uncertain old applications, not just the first one.
        other = store.ledger.import_historical_attempt(**{**kwargs, "source_id": "synthetic-legacy:record-2",
                                                          "job_id": 18, "requisition": "REQ-456"})
        store.ledger.confirm(other["submission_attempt_id"], now=NOW, confirmed=True)
        assert store.ledger.status(now="2026-09-06T12:00:00Z")["reason"] == "submission_pending"
    raw = store.path.read_bytes()
    assert kwargs["source_id"].encode() not in raw
    assert IDENTITY["account_id"].encode() not in raw
    assert IDENTITY["tenant"].encode() not in raw


@pytest.mark.parametrize("mutation", [
    {"trusted": False}, {"source_id": ""}, {"evidence_sha256": "not-a-digest"},
    {"state": "released"}, {"state": "confirmed"}, {"confirmed_at": NOW},
    {"state": "confirmed", "confirmed_at": "2026-09-05T11:59:59Z"},
])
def test_import_rejects_untrusted_or_incoherent_history_without_writing(tmp_path, mutation):
    store = SubmissionAuthorizationStore(tmp_path / "shared.sqlite3")
    kwargs = dict(source_id="synthetic-source", evidence_sha256="a" * 64, trusted=True,
                  job_id=17, requisition="REQ-123", state="unknown", attempted_at=NOW,
                  confirmed_at=None, **IDENTITY)
    with pytest.raises(ValueError, match="historical"):
        store.ledger.import_historical_attempt(**{**kwargs, **mutation})
    assert store.ledger.status(now=NOW)["eligible"] is True


def test_import_source_conflict_does_not_overwrite_or_forget_uncertainty(tmp_path):
    store = SubmissionAuthorizationStore(tmp_path / "shared.sqlite3")
    kwargs = dict(source_id="synthetic-source", evidence_sha256="a" * 64, trusted=True,
                  job_id=17, requisition="REQ-123", state="unknown", attempted_at=NOW, **IDENTITY)
    original = store.ledger.import_historical_attempt(**kwargs)
    with pytest.raises(PermissionError, match="conflict"):
        store.ledger.import_historical_attempt(**{**kwargs, "requisition": "REQ-OTHER"})
    assert store.ledger.import_historical_attempt(**kwargs) == original


@pytest.mark.parametrize("transition", ["mark_dispatch", "confirm", "release_before_dispatch"])
def test_transition_clock_cannot_move_before_durable_intent(tmp_path, transition):
    store = SubmissionAuthorizationStore(tmp_path / "shared.sqlite3")
    attempt = consume(store, issue(store))
    attempt_id = attempt["submission_attempt_id"]
    if transition == "confirm":
        store.ledger.mark_dispatch(attempt_id, now=NOW)
    kwargs = {"confirmed": True} if transition == "confirm" else {}
    with pytest.raises(ValueError, match="precede"):
        getattr(store.ledger, transition)(attempt_id, now="2026-09-05T11:59:59Z", **kwargs)
    assert store.ledger.status(now=NOW)["reason"] == "submission_pending"


def test_legacy_consumed_authorization_is_migrated_as_unknown_not_fresh_on_restart(tmp_path):
    import sqlite3

    database = tmp_path / "old.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE submission_authorizations (
            token_digest TEXT PRIMARY KEY, job_id INTEGER, target_id TEXT, page_url TEXT,
            requisition TEXT, review_evidence_sha256 TEXT, actor TEXT, issued_at TEXT,
            expires_at TEXT, used_at TEXT, invalidated_at TEXT, invalidation_reason TEXT)""")
        connection.execute("""INSERT INTO submission_authorizations VALUES
            (?, 17, 'old-target', 'https://synthetic.test', 'REQ-123', ?, 'synthetic-actor', ?, ?, ?, NULL, NULL)""",
            ("b" * 64, "a" * 64, NOW, LATER, NOW))
    store = SubmissionAuthorizationStore(database)
    assert store.ledger.status(now="2026-09-06T12:00:00Z")["reason"] == "submission_pending"
    with pytest.raises(PermissionError, match="application"):
        issue(store)
    attempt = store.ledger.attempt_for_application(job_id=17, requisition="REQ-123")
    assert attempt["state"] == "unknown"
    restarted = SubmissionAuthorizationStore(database)
    assert restarted.ledger.attempt_for_application(job_id=17, requisition="REQ-123") == attempt


def _concurrent_consumer(database, index, mode, barrier, results, shared_token):
    """Spawned OS processes exercise actual SQLite locks, no fake stores."""
    try:
        store = SubmissionAuthorizationStore(Path(database).with_name(f"run-{index}.sqlite3"), ledger_path=database)
        issued = shared_token or issue(store, job_id=17 + index if mode == "different_jobs" else 17,
                                      requisition=f"REQ-{index}" if mode == "different_jobs" else "REQ-123", **IDENTITY)
        barrier.wait(timeout=15)
        try:
            consumed = consume(store, issued, **IDENTITY)
            results.put(("acquired", consumed["submission_attempt_id"]))
        except PermissionError:
            results.put(("denied", None))
    except BaseException as error:
        results.put(("error", type(error).__name__))


@pytest.mark.parametrize("mode", ["same_token", "different_tokens", "different_jobs"])
def test_concurrent_processes_reserve_only_one_global_attempt(tmp_path, mode):
    import multiprocessing

    database = tmp_path / "global.sqlite3"
    shared = issue(SubmissionAuthorizationStore(database), **IDENTITY) if mode == "same_token" else None
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(4), context.Queue()
    workers = [context.Process(target=_concurrent_consumer, args=(str(database), index, mode, barrier, results, shared))
               for index in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=25)
            assert worker.exitcode == 0
        outcomes = [results.get(timeout=5)[0] for _ in workers]
        assert outcomes.count("acquired") == 1
        assert outcomes.count("denied") == 3
        assert SubmissionAuthorizationStore(database).ledger.status(now=LATER)["reason"] == "submission_pending"
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
        results.close()


def _crash_at_boundary(database, issued, boundary):
    import os
    import submission_ledger

    store = SubmissionAuthorizationStore(database)
    if boundary == "before_reservation_commit":
        reserve = submission_ledger.reserve
        def interrupted_reserve(*args, **kwargs):
            reserve(*args, **kwargs)
            os._exit(23)
        submission_ledger.reserve = interrupted_reserve
    consumed = consume(store, issued, **IDENTITY)
    if boundary == "after_reservation_commit":
        os._exit(23)
    store.ledger.mark_dispatch(consumed["submission_attempt_id"], now=NOW)
    if boundary in {"before_browser_dispatch", "after_browser_dispatch"}:
        # A durable synthetic click marker represents an external side effect;
        # no browser is opened. Both sides of this boundary must be unknown.
        if boundary == "after_browser_dispatch":
            Path(database).with_suffix(".synthetic-click").write_text("one click", encoding="utf-8")
        os._exit(23)
    store.ledger.confirm(consumed["submission_attempt_id"], now=NOW, confirmed=True)
    os._exit(23)


@pytest.mark.parametrize("boundary,expected_state", [
    ("before_reservation_commit", None), ("after_reservation_commit", "reserved"),
    ("before_browser_dispatch", "unknown"), ("after_browser_dispatch", "unknown"),
    ("after_confirmation_commit", "confirmed"),
])
def test_process_crash_boundaries_never_infer_safe_replay(tmp_path, boundary, expected_state):
    import multiprocessing

    database = tmp_path / "global.sqlite3"
    store = SubmissionAuthorizationStore(database)
    issued = issue(store, **IDENTITY)
    other = issue(store, job_id=18, requisition="REQ-456", **IDENTITY)
    context = multiprocessing.get_context("spawn")
    worker = context.Process(target=_crash_at_boundary, args=(str(database), issued, boundary))
    try:
        worker.start()
        worker.join(timeout=25)
        assert worker.exitcode == 23
        restarted = SubmissionAuthorizationStore(database)
        attempt = restarted.ledger.attempt_for_application(job_id=17, requisition="REQ-123", **IDENTITY)
        assert (attempt["state"] if attempt else None) == expected_state
        if expected_state is None:
            assert restarted.ledger.status(now=NOW)["eligible"] is True
            consume(restarted, issued, **IDENTITY)  # no commit, token AND fence rolled back
        else:
            with pytest.raises(PermissionError):
                consume(restarted, other, **IDENTITY)
            with pytest.raises(PermissionError, match="replayed"):
                consume(restarted, issued, **IDENTITY)
            if expected_state in {"reserved", "unknown"}:
                assert restarted.ledger.status(now="2026-10-01T12:00:00Z")["reason"] == "submission_pending"
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)


@pytest.mark.parametrize("changed", [{"account_id": "other-account"}, {"tenant": "other-tenant"}])
def test_immutable_identity_drift_permanently_invalidates_token(tmp_path, changed):
    store = SubmissionAuthorizationStore(tmp_path / "shared.sqlite3")
    issued = issue(store, **IDENTITY)
    with pytest.raises(PermissionError, match="identity drift"):
        consume(store, issued, **{**IDENTITY, **changed})
    with pytest.raises(PermissionError, match="invalidated"):
        consume(store, issued, **IDENTITY)
    assert store.ledger.status(now=NOW)["eligible"] is True


def test_unknown_suspends_distinct_jobs_across_restart_until_confirmed_then_hourly_limit(tmp_path):
    database = tmp_path / "shared.sqlite3"
    store = SubmissionAuthorizationStore(database)
    first = issue(store, **IDENTITY)
    second = issue(store, job_id=18, requisition="REQ-456", until="2026-09-10T15:00:00Z", **IDENTITY)
    attempt = consume(store, first, **IDENTITY)
    # No hourly timeout may release a reserved or unknown operation.
    restarted = SubmissionAuthorizationStore(database)
    with pytest.raises(PermissionError, match="pending"):
        consume(restarted, second, now="2026-09-06T12:00:00Z", **IDENTITY)
    ledger = restarted.ledger
    assert ledger.status(now="2026-09-06T12:00:00Z")["reason"] == "submission_pending"
    attempt_id = attempt["submission_attempt_id"]
    ledger.mark_dispatch(attempt_id, now=NOW)
    ledger.confirm(attempt_id, now="2026-09-06T12:00:00Z", confirmed=True)
    ledger.confirm(attempt_id, now="2026-09-06T12:30:00Z", confirmed=True)
    assert ledger.status(now="2026-09-06T12:59:59Z") == {
        "eligible": False, "reason": "hourly_limit", "pending_attempt_id": None,
        "next_eligible_at": "2026-09-06T13:00:00+00:00",
    }
    with pytest.raises(PermissionError, match="hourly"):
        consume(restarted, second, now="2026-09-06T12:59:59Z", **IDENTITY)
    assert ledger.status(now="2026-09-06T13:00:00Z")["eligible"] is True
    # Failed reservations above rolled token consumption back.
    later = consume(restarted, second, now="2026-09-10T12:00:00Z", **IDENTITY)
    ledger.mark_dispatch(later["submission_attempt_id"], now="2026-09-10T12:00:00Z")
    ledger.confirm(later["submission_attempt_id"], now="2026-09-10T12:00:00Z", confirmed=True)
    third = issue(store, job_id=19, requisition="REQ-789", at="2026-09-10T12:00:00Z",
                  until="2026-09-10T15:00:00Z", **IDENTITY)
    with pytest.raises(PermissionError, match="hourly"):
        consume(store, third, now="2026-09-10T12:00:01Z", **IDENTITY)
