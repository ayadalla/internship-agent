from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


PAGE_URL = "https://sanitized.example.test/apply/REQ-123"


def review_artifact() -> dict:
    artifact = {
        "review_authoritative": True,
        "submission_authorized": False,
        "binding": {
            "target_id": "page-42",
            "page_url": PAGE_URL,
            "company": "Sanitized Example",
            "role": "Software Engineer Intern",
            "requisition": "REQ-123",
            "verified": True,
        },
        "human_required": [],
        "evidence": {"sanitized": True, "review_authority_only": True},
    }
    canonical = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    artifact["review_evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return artifact


def issue_authorization(tmp_path, **identity):
    import submission_authorization

    review = review_artifact()
    store = submission_authorization.SubmissionAuthorizationStore(tmp_path / "authorization.db")
    issued = store.issue(
        job_id=17,
        review_evidence=review,
        actor="fixture-operator",
        issued_at="2026-08-27T08:00:00+00:00",
        expires_at="2026-08-27T08:05:00+00:00",
        **identity,
    )
    return store, issued["token"], review["review_evidence_sha256"]


class ExactSubmitPage:
    def __init__(self, journal_path: Path, *, gates=None, maango=False, interruption=False):
        self.journal_path = journal_path
        self.gates = list(gates or [])
        self.maango = maango
        self.interruption = interruption
        self.click_count = 0
        self.confirmed = not interruption

    def read_only_snapshot(self) -> dict:
        return {
            "read_only": True,
            "target_id": "page-42",
            "url": PAGE_URL,
            "identity": {"requisition": "REQ-123"},
            "gates": self.gates,
            "maango": self.maango,
        }

    def inspect_submit_control(self, selector: str) -> dict:
        return {
            "selector": selector,
            "target_id": "page-42",
            "url": PAGE_URL,
            "visible": True,
            "enabled": True,
            "unique": True,
            "role": "button",
        }

    def click_submit_once(self, selector: str) -> None:
        intent = json.loads(self.journal_path.read_text(encoding="utf-8").splitlines()[-1])
        assert intent["action"] == "submit"
        assert intent["evidence"]["status"] == "intent_recorded"
        assert intent["evidence"]["verified"] is False
        self.click_count += 1
        if self.interruption:
            import one_shot_submit

            raise one_shot_submit.SubmitInterrupted("fixture connection interrupted")

    def inspect_confirmation(self) -> dict:
        return {"confirmed": self.confirmed, "state": "submitted" if self.confirmed else "unknown"}


def execute(operator_module, *, store, token, page, journal_path, review_hash, maango_approved=False, **kwargs):
    return operator_module.execute_one_shot_submit(
        authorization_store=store,
        token=token,
        page=page,
        journal_path=journal_path,
        job_id=17,
        target_id="page-42",
        expected_url=PAGE_URL,
        requisition="REQ-123",
        review_evidence_sha256=review_hash,
        actor="fixture-operator",
        now="2026-08-27T08:01:00+00:00",
        submit_selector="#submit-application",
        maango_approved=maango_approved,
        **kwargs,
    )


def test_one_shot_submit_consumes_authorization_journals_intent_then_clicks_once(tmp_path):
    import one_shot_submit

    journal_path = tmp_path / "page-actions.jsonl"
    store, token, review_hash = issue_authorization(tmp_path)
    page = ExactSubmitPage(journal_path)

    result = execute(
        one_shot_submit,
        store=store,
        token=token,
        page=page,
        journal_path=journal_path,
        review_hash=review_hash,
    )

    assert result["status"] == "confirmation_observed"
    assert result["authorization_consumed"] is True
    assert result["one_shot"] is True
    assert result["replay_allowed"] is False
    assert page.click_count == 1
    entries = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["evidence"]["verified"] for entry in entries] == [False, True]
    with pytest.raises(PermissionError, match="replayed"):
        execute(
            one_shot_submit,
            store=store,
            token=token,
            page=page,
            journal_path=journal_path,
            review_hash=review_hash,
        )
    assert page.click_count == 1


@pytest.mark.parametrize("boundary", ["journal", "final_snapshot", "final_control"])
def test_known_failure_before_dispatch_releases_reservation_but_burns_token(tmp_path, monkeypatch, boundary):
    import one_shot_submit

    journal = tmp_path / "actions.jsonl"
    store, token, review_hash = issue_authorization(tmp_path)
    page = ExactSubmitPage(journal)
    if boundary == "journal":
        def fail_journal(*args, **kwargs):
            raise OSError("private-sentinel-journal")
        monkeypatch.setattr(one_shot_submit, "record_page_action", fail_journal)
    else:
        method = "read_only_snapshot" if boundary == "final_snapshot" else "inspect_submit_control"
        original = getattr(page, method)
        calls = []
        def fail_final(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("private-sentinel-browser")
            return original(*args)
        setattr(page, method, fail_final)
    result = execute(one_shot_submit, store=store, token=token, page=page,
                     journal_path=journal, review_hash=review_hash)
    assert result["status"] == "blocked"
    assert "private-sentinel" not in json.dumps(result)
    assert page.click_count == 0
    assert store.ledger.attempt_for_application(job_id=17, requisition="REQ-123")["state"] == "released"
    assert store.ledger.status(now="2026-08-27T08:01:00Z")["eligible"] is True
    # Rollback is for reservation only; old authorization is never reusable.
    with pytest.raises(PermissionError, match="replayed"):
        store.consume(token=token, actor="fixture-operator", now="2026-08-27T08:01:00Z",
                      current_binding={})
    _, replacement, _ = issue_authorization(tmp_path)
    assert replacement != token


@pytest.mark.parametrize("boundary", ["dispatch", "inspection", "confirmation_commit", "confirmation_journal", "dispatch_commit"])
def test_post_dispatch_failures_never_release_or_log_exception_secrets(tmp_path, monkeypatch, capsys, boundary):
    import one_shot_submit

    journal = tmp_path / "actions.jsonl"
    store, token, review_hash = issue_authorization(tmp_path)
    page = ExactSubmitPage(journal)
    secret = "synthetic-secret-not-for-logs"
    def fail(*args, **kwargs):
        raise RuntimeError(secret)
    if boundary == "dispatch":
        page.click_submit_once = fail
        page.confirmed = False
    elif boundary == "inspection":
        page.inspect_confirmation = fail
    elif boundary == "confirmation_commit":
        monkeypatch.setattr(store.ledger, "confirm", fail)
    elif boundary == "dispatch_commit":
        real_mark = store.ledger.mark_dispatch
        def committed_then_failed(*args, **kwargs):
            real_mark(*args, **kwargs)
            fail()
        monkeypatch.setattr(store.ledger, "mark_dispatch", committed_then_failed)
    else:
        real_record = one_shot_submit.record_page_action
        def fail_confirmation(*args, **kwargs):
            if kwargs["evidence"]["verified"]:
                fail()
            real_record(*args, **kwargs)
        monkeypatch.setattr(one_shot_submit, "record_page_action", fail_confirmation)
    result = execute(one_shot_submit, store=store, token=token, page=page,
                     journal_path=journal, review_hash=review_hash)
    assert result["replay_allowed"] is False
    attempt = store.ledger.attempt_for_application(job_id=17, requisition="REQ-123")
    assert attempt["state"] in {"unknown", "confirmed"}
    with pytest.raises(PermissionError, match="cannot release"):
        store.ledger.release_before_dispatch(attempt["submission_attempt_id"], now="2026-08-27T08:02:00Z")
    assert store.ledger.status(now="2026-08-27T08:02:00Z")["eligible"] is False
    assert secret not in json.dumps(result) + journal.read_text(encoding="utf-8") + capsys.readouterr().out
    assert token not in journal.read_text(encoding="utf-8")
    assert token.encode() not in store.path.read_bytes()


@pytest.mark.parametrize("dispatch_at,expected_clicks", [("08:04:00", 1), ("08:05:00", 0)])
def test_injected_clock_rechecks_dispatch_freshness_and_uses_observed_confirmation_time(tmp_path, dispatch_at, expected_clicks):
    import one_shot_submit

    identity = {"account_id": "synthetic-account", "tenant": "synthetic-tenant"}
    store, token, review_hash = issue_authorization(tmp_path, **identity)
    journal = tmp_path / "actions.jsonl"
    page = ExactSubmitPage(journal)
    times = iter(["2026-08-27T08:01:00Z", f"2026-08-27T{dispatch_at}Z", "2026-08-27T08:06:00Z"])
    result = execute(one_shot_submit, store=store, token=token, page=page,
                     journal_path=journal, review_hash=review_hash, clock=lambda: next(times), **identity)
    assert page.click_count == expected_clicks
    attempt = store.ledger.attempt_for_application(job_id=17, requisition="REQ-123", **identity)
    assert attempt["state"] == ("confirmed" if expected_clicks else "released")
    if expected_clicks:
        assert result["status"] == "confirmation_observed"
        assert attempt["confirmed_at"] == "2026-08-27T08:06:00+00:00"
        assert store.ledger.status(now="2026-08-27T09:05:59Z")["next_eligible_at"] == "2026-08-27T09:06:00+00:00"


def test_legacy_authorizer_cannot_silently_skip_transactional_fence(tmp_path):
    import one_shot_submit

    class LegacyStore:
        called = False
        def consume(self, **kwargs):
            self.called = True
            return {"authorization_consumed": True}
    store = LegacyStore()
    journal = tmp_path / "actions.jsonl"
    page = ExactSubmitPage(journal)
    with pytest.raises(PermissionError, match="transactional"):
        execute(one_shot_submit, store=store, token="synthetic-token", page=page,
                journal_path=journal, review_hash="a" * 64)
    assert store.called is False
    assert page.click_count == 0


def test_production_requires_refreshable_clock_before_any_consumption(tmp_path, monkeypatch):
    import one_shot_submit
    import submission_ledger
    from submission_authorization import SubmissionAuthorizationStore

    canonical = tmp_path / "production-synthetic.sqlite3"
    monkeypatch.setattr(submission_ledger, "CANONICAL_LEDGER_PATH", canonical)
    store = SubmissionAuthorizationStore(canonical, production=True)
    identity = {"account_id": "synthetic-account", "tenant": "synthetic-tenant"}
    review = review_artifact()
    issued = store.issue(job_id=17, review_evidence=review, actor="fixture-operator",
                         issued_at="2026-08-27T08:00:00Z", expires_at="2026-08-27T08:05:00Z", **identity)
    journal = tmp_path / "actions.jsonl"
    page = ExactSubmitPage(journal)
    with pytest.raises(one_shot_submit.SubmitBlockedError, match="clock"):
        execute(one_shot_submit, store=store, token=issued["token"], page=page,
                journal_path=journal, review_hash=review["review_evidence_sha256"], **identity)
    assert store.ledger.status(now="2026-08-27T08:01:00Z")["eligible"] is True
    assert page.click_count == 0
    result = execute(one_shot_submit, store=store, token=issued["token"], page=page,
                     journal_path=journal, review_hash=review["review_evidence_sha256"],
                     clock=lambda: "2026-08-27T08:01:00Z", **identity)
    assert result["status"] == "confirmation_observed"


@pytest.mark.parametrize("surface", ["snapshot", "control", "gate"])
def test_preflight_never_exposes_raw_browser_secrets(tmp_path, surface):
    import one_shot_submit

    store, token, review_hash = issue_authorization(tmp_path)
    journal = tmp_path / "actions.jsonl"
    page = ExactSubmitPage(journal)
    secret = "synthetic-private-preflight"
    def fail(*args):
        raise RuntimeError(secret)
    if surface == "gate":
        page.gates = [secret]
    else:
        setattr(page, "read_only_snapshot" if surface == "snapshot" else "inspect_submit_control", fail)
    with pytest.raises(one_shot_submit.SubmitBlockedError) as error:
        execute(one_shot_submit, store=store, token=token, page=page,
                journal_path=journal, review_hash=review_hash)
    assert secret not in str(error.value)
    assert page.click_count == 0
    assert store.ledger.status(now="2026-08-27T08:01:00Z")["eligible"] is True


def test_durable_dispatch_and_confirmation_update_shared_ledger(tmp_path):
    import one_shot_submit

    journal = tmp_path / "actions.jsonl"
    store, token, review_hash = issue_authorization(tmp_path)

    class DurablePage(ExactSubmitPage):
        def click_submit_once(self, selector):
            attempt = store.ledger.attempt_for_application(job_id=17, requisition="REQ-123")
            assert attempt["state"] == "unknown", "dispatch uncertainty must commit before click"
            assert store.ledger.status(now="2026-08-28T08:00:00Z")["eligible"] is False
            super().click_submit_once(selector)

    page = DurablePage(journal)
    execute(one_shot_submit, store=store, token=token, page=page,
            journal_path=journal, review_hash=review_hash)
    attempt = store.ledger.attempt_for_application(job_id=17, requisition="REQ-123")
    assert attempt["state"] == "confirmed"
    assert attempt["confirmed_at"] == "2026-08-27T08:01:00+00:00"
    assert store.ledger.status(now="2026-08-27T08:59:00Z")["reason"] == "hourly_limit"


def test_distinct_tokens_cannot_click_same_application_after_unknown(tmp_path):
    import one_shot_submit

    store, first_token, review_hash = issue_authorization(tmp_path)
    _, second_token, _ = issue_authorization(tmp_path)
    assert first_token != second_token
    journal = tmp_path / "actions.jsonl"
    page = ExactSubmitPage(journal, interruption=True)
    first = execute(one_shot_submit, store=store, token=first_token, page=page,
                    journal_path=journal, review_hash=review_hash)
    assert first["status"] == "blocked"
    # A new process/handoff must not turn uncertainty into a second click.
    try:
        execute(one_shot_submit, store=store, token=second_token, page=page,
                journal_path=journal, review_hash=review_hash)
    except PermissionError:
        pass
    assert page.click_count == 1


def test_submit_interruption_inspects_confirmation_and_never_replays(tmp_path):
    import one_shot_submit

    journal_path = tmp_path / "page-actions.jsonl"
    store, token, review_hash = issue_authorization(tmp_path)
    page = ExactSubmitPage(journal_path, interruption=True)

    result = execute(
        one_shot_submit,
        store=store,
        token=token,
        page=page,
        journal_path=journal_path,
        review_hash=review_hash,
    )

    assert result == {
        "status": "blocked",
        "blocker": "submit interrupted without confirmation",
        "next_action": "inspect_confirmation_without_replay",
        "authorization_consumed": True,
        "one_shot": True,
        "replay_allowed": False,
    }
    assert page.click_count == 1
    with pytest.raises(PermissionError, match="replayed"):
        execute(
            one_shot_submit,
            store=store,
            token=token,
            page=page,
            journal_path=journal_path,
            review_hash=review_hash,
        )
    assert page.click_count == 1


@pytest.mark.parametrize("gate", ["captcha", "email_verification", "passkey", "mfa_approval", "assessment", "identity_verification"])
def test_submit_rejects_mandatory_human_gate_before_authorization_or_click(tmp_path, gate):
    import one_shot_submit

    class NeverConsume:
        def consume(self, **kwargs):
            pytest.fail("authorization must not be consumed")

    journal_path = tmp_path / "page-actions.jsonl"
    page = ExactSubmitPage(journal_path, gates=[gate])

    with pytest.raises(one_shot_submit.SubmitBlockedError, match=gate):
        execute(
            one_shot_submit,
            store=NeverConsume(),
            token="unused",
            page=page,
            journal_path=journal_path,
            review_hash="a" * 64,
        )

    assert page.click_count == 0
    assert not journal_path.exists()


def test_submit_rejects_unapproved_maango_or_non_exact_control(tmp_path):
    import one_shot_submit

    class NeverConsume:
        def consume(self, **kwargs):
            pytest.fail("authorization must not be consumed")

    journal_path = tmp_path / "page-actions.jsonl"
    maango_page = ExactSubmitPage(journal_path, maango=True)
    with pytest.raises(one_shot_submit.SubmitBlockedError, match="MAANGO approval"):
        execute(
            one_shot_submit,
            store=NeverConsume(),
            token="unused",
            page=maango_page,
            journal_path=journal_path,
            review_hash="a" * 64,
        )

    hidden_page = ExactSubmitPage(journal_path)
    hidden_page.inspect_submit_control = lambda selector: {
        "selector": selector,
        "target_id": "page-42",
        "url": PAGE_URL,
        "visible": False,
        "enabled": True,
        "unique": True,
        "role": "button",
    }
    with pytest.raises(one_shot_submit.SubmitBlockedError, match="visible, enabled, unique button"):
        execute(
            one_shot_submit,
            store=NeverConsume(),
            token="unused",
            page=hidden_page,
            journal_path=journal_path,
            review_hash="a" * 64,
        )
