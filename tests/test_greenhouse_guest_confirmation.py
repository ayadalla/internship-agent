"""SYNTHETIC local fixtures; never evidence of a live Greenhouse submission."""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import live_confirmation_reader
from page_recovery import record_page_action

ORIGIN = "https://job-boards.greenhouse.io/schonfeld/jobs/8171772"
THANKYOU = "https://job-boards.greenhouse.io/schonfeld/thank_you"
TARGET = "synthetic-target-not-a-live-tab"
IDENTITY = {"company": "Schonfeld", "role": "2027 DMFI Technology Intern", "requisition": "P101884-2026-09-01"}
SUCCESS = "Thank you for applying! Your application has been received."
HTML = f"<html><body><main><h1>{SUCCESS}</h1></main></body></html>"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_digest(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")))


def guest_context(tmp_path):
    review = {
        "review_authoritative": True,
        "submission_authorized": False,
        "binding": {"target_id": TARGET, "page_url": ORIGIN, **IDENTITY, "verified": True},
        "human_required": [],
        "evidence": {"sanitized": True},
    }
    review["review_evidence_sha256"] = canonical_digest(review)
    intent = {
        "status": "intent_recorded", "verified": False,
        "job_id": 8171772, "target_id": TARGET, "page_url": ORIGIN,
        "requisition": IDENTITY["requisition"],
        "review_evidence_sha256": review["review_evidence_sha256"],
        "selector": "button[type=submit]",
    }
    journal = tmp_path / "synthetic-page-actions.jsonl"
    record_page_action(journal, action="submit", evidence=intent)
    return {
        "job_id": 8171772, "journal_path": journal, "review_evidence": review,
        "transition": {
            "source": "one_shot_same_tab_observation", "trusted": True,
            "target_id": TARGET, "from_url": ORIGIN, "to_url": THANKYOU,
            "intent_sha256": canonical_digest(intent),
            "before_html_sha256": digest("<html><body>synthetic original form</body></html>"),
            "after_html_sha256": digest(HTML), "after_body_text_sha256": digest(SUCCESS),
        },
    }


class ObservedPage:
    """Only synthetic observations; deliberately lacks all mutation methods."""

    def __init__(self, *, url=THANKYOU, html=HTML, body_text=SUCCESS, target_id=TARGET):
        self.snapshot = {"read_only": True, "target_id": target_id, "url": url, "html": html, "body_text": body_text}
        self.reads = 0

    def read_only_snapshot(self):
        self.reads += 1
        return copy.deepcopy(self.snapshot)

    def read_candidate_applications(self):
        pytest.fail("guest confirmation must not invent or read Candidate Home")


def reconcile(context, page=None, **overrides):
    kwargs = dict(page=page or ObservedPage(), platform="greenhouse", tenant="schonfeld", target_id=TARGET,
                  expected_url=ORIGIN, expected_identity=IDENTITY, guest_submission=context)
    kwargs.update(overrides)
    return live_confirmation_reader.read_and_reconcile(**kwargs)


def test_guest_explicit_success_uses_original_intent_not_nonexistent_candidate_home(tmp_path):
    context = guest_context(tmp_path)
    journal_before = context["journal_path"].read_bytes()
    result = reconcile(context)

    assert result["portal_confirmed"] is True
    assert result["safe_for_post_submit"] is True
    assert result["confirmation_basis"] == "greenhouse_guest_one_shot"
    assert result["identity"] == IDENTITY
    assert result["confirmation"]["url"] == THANKYOU
    assert result["confirmation"]["reference_id"] is None
    assert result["confirmation"]["submitted"] is True
    assert result["portal_readback"] == {
        "matched_application_count": 0, "state": "", "submitted": False, "verified": False, "applicable": False,
    }
    assert result["evidence"]["two_source_reconciliation"] is False
    assert result["reader"] == {"platform": "greenhouse", "tenant": "schonfeld", "verified": True, "mode": "guest_one_shot"}
    assert result["human_required"] == []
    assert result["replay_allowed"] is False
    assert SUCCESS not in json.dumps(result)
    assert "<html" not in json.dumps(result)
    assert context["journal_path"].read_bytes() == journal_before


@pytest.mark.parametrize("damage", [
    "missing_journal", "empty_journal", "duplicate_intent", "wrong_job_intent",
    "wrong_target_intent", "wrong_origin_intent", "wrong_req_intent", "wrong_review_intent",
    "unverified_intent", "unmatched_followup", "malformed_journal",
    "untrusted_transition", "wrong_transition_source", "wrong_transition_origin",
    "wrong_transition_target", "wrong_transition_intent", "stale_html", "stale_body",
    "unchanged_page", "missing_before_hash", "tampered_review", "non_authoritative_review",
    "wrong_review_company", "wrong_review_role", "wrong_review_req", "wrong_review_target", "wrong_review_origin",
])
def test_guest_requires_exact_original_review_intent_and_trusted_transition(tmp_path, damage):
    context = guest_context(tmp_path)
    journal = context["journal_path"]
    entry = json.loads(journal.read_text(encoding="utf-8"))
    intent_fields = {
        "wrong_job_intent": "job_id", "wrong_target_intent": "target_id",
        "wrong_origin_intent": "page_url", "wrong_req_intent": "requisition",
        "wrong_review_intent": "review_evidence_sha256", "unverified_intent": "verified",
    }
    transition_fields = {
        "untrusted_transition": "trusted", "wrong_transition_source": "source",
        "wrong_transition_origin": "from_url", "wrong_transition_target": "target_id",
        "wrong_transition_intent": "intent_sha256", "stale_html": "after_html_sha256",
        "stale_body": "after_body_text_sha256", "missing_before_hash": "before_html_sha256",
    }
    if damage == "missing_journal":
        journal.unlink()
    elif damage == "empty_journal":
        journal.write_text("", encoding="utf-8")
    elif damage == "duplicate_intent":
        record_page_action(journal, action="submit", evidence=entry["evidence"])
    elif damage in intent_fields:
        entry["evidence"][intent_fields[damage]] = "wrong"
        journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    elif damage == "unmatched_followup":
        record_page_action(journal, action="submit", evidence={**entry["evidence"], "status": "confirmation_observed", "target_id": "unrelated", "verified": True})
    elif damage == "malformed_journal":
        journal.write_text('{"truncated":', encoding="utf-8")
    elif damage in transition_fields:
        context["transition"][transition_fields[damage]] = "wrong"
    elif damage == "unchanged_page":
        context["transition"]["before_html_sha256"] = context["transition"]["after_html_sha256"]
    elif damage == "tampered_review":
        context["review_evidence"]["binding"]["company"] = "unrelated"
    else:
        review = context["review_evidence"]
        if damage == "non_authoritative_review":
            review["review_authoritative"] = False
        else:
            field = {"company": "company", "role": "role", "req": "requisition", "target": "target_id", "origin": "page_url"}[damage.removeprefix("wrong_review_")]
            review["binding"][field] = "wrong"
        review.pop("review_evidence_sha256")
        review["review_evidence_sha256"] = canonical_digest(review)
        entry["evidence"]["review_evidence_sha256"] = review["review_evidence_sha256"]
        journal.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        context["transition"]["intent_sha256"] = canonical_digest(entry["evidence"])

    result = reconcile(context)

    assert result["portal_confirmed"] is False, damage
    assert result["safe_for_post_submit"] is False
    assert result["human_required"]
    assert result["replay_allowed"] is False
    assert result["next_action"] == "inspect_confirmation_without_replay"


@pytest.mark.parametrize("url,allowed", [
    (THANKYOU, True), (ORIGIN, True), (ORIGIN + "/confirmation", True),
    (ORIGIN + "/thank_you", True),
    ("https://job-boards.greenhouse.io/other/thank_you", False),
    ("https://job-boards.greenhouse.io/schonfeld/jobs/999/confirmation", False),
    ("https://job-boards.greenhouse.io/schonfeld", False),
    ("https://job-boards.greenhouse.io/thank_you", False),
    ("https://job-boards.greenhouse.io.evil.test/schonfeld/thank_you", False),
    ("https://job-boards.greenhouse.io@evil.test/schonfeld/thank_you", False),
    ("https://evil.test/schonfeld/thank_you", False),
    ("http://job-boards.greenhouse.io/schonfeld/thank_you", False),
    ("https://job-boards.greenhouse.io:443/schonfeld/thank_you", False),
    (THANKYOU + "?job_id=999", False), (THANKYOU + "#application-received", False),
    ("https://boards.greenhouse.io/schonfeld/thank_you", False),
    ("https://job-boards.greenhouse.io/schonfeld/../other/thank_you", False),
])
def test_guest_only_permits_exact_origin_or_scoped_greenhouse_success_routes(tmp_path, url, allowed):
    import greenhouse_guest_confirmation

    helper = getattr(greenhouse_guest_confirmation, "is_permitted_confirmation_url", None)
    assert callable(helper), "strict route helper is required for worker transport integration"
    assert helper(origin_url=ORIGIN, page_url=url, tenant="schonfeld") is allowed
    context = guest_context(tmp_path)
    context["transition"]["to_url"] = url
    result = reconcile(context, ObservedPage(url=url))
    assert result["portal_confirmed"] is allowed


def test_guest_route_is_bound_to_manifest_tenant(tmp_path):
    result = reconcile(guest_context(tmp_path), tenant="other")
    assert result["portal_confirmed"] is False


@pytest.mark.parametrize("html,body", [
    ('<html><script>"Thank you for applying"</script><body>Complete application</body></html>', "Complete application"),
    ('<html><body><template>Thank you for applying</template><p>Complete application</p></body></html>', "Complete application"),
    ('<html><head><title>Thank you for applying</title></head><body>Complete application</body></html>', "Complete application"),
    ('<html><body><div hidden>Thank you for applying</div></body></html>', "Thank you for applying"),
    ('<html><body><div style="display:none">Thank you for applying</div></body></html>', "Thank you for applying"),
    ('<html><body><div aria-hidden="true">Thank you for applying</div></body></html>', "Thank you for applying"),
    ('<html><body><p>When successful, you will see: Thank you for applying.</p></body></html>', "When successful, you will see: Thank you for applying."),
    ('<html><body><p>No application received.</p></body></html>', "No application received."),
    ('<html><body><p>Application received? Not yet.</p></body></html>', "Application received? Not yet."),
    (HTML, "Please complete the form"),
    ('<html><body>Unrelated page</body></html>', SUCCESS),
])
def test_guest_requires_actual_rendered_unambiguous_success_not_hidden_or_future_text(tmp_path, html, body):
    context = guest_context(tmp_path)
    context["transition"]["after_html_sha256"] = digest(html)
    context["transition"]["after_body_text_sha256"] = digest(body)
    result = reconcile(context, ObservedPage(html=html, body_text=body))
    assert result["portal_confirmed"] is False
    assert result["replay_allowed"] is False


@pytest.mark.parametrize("text", [
    "Application submitted.", "Your application has been submitted.",
    "Your application has been successfully submitted!", "We have received your application.",
    "We've received your application.", "Application submitted successfully.",
])
def test_guest_recognizes_explicit_application_receipt_statements(tmp_path, text):
    context = guest_context(tmp_path)
    html = f"<html><body><main><h1>{text}</h1></main></body></html>"
    context["transition"]["after_html_sha256"] = digest(html)
    context["transition"]["after_body_text_sha256"] = digest(text)
    result = reconcile(context, ObservedPage(html=html, body_text=text))
    assert result["portal_confirmed"] is True
    assert result["confirmation"]["text_sha256"] == digest(text)


def test_guest_result_preserves_sanitized_original_intent_and_observation_provenance(tmp_path):
    context = guest_context(tmp_path)
    intent = json.loads(context["journal_path"].read_text(encoding="utf-8"))["evidence"]
    result = reconcile(context)
    assert result["evidence"]["provenance"] == {
        "source": "one_shot_same_tab_observation",
        "binding": {key: intent[key] for key in ("job_id", "target_id", "page_url", "requisition", "review_evidence_sha256")},
        **{key: context["transition"][key] for key in (
            "intent_sha256", "before_html_sha256", "after_html_sha256", "after_body_text_sha256",
        )},
    }


@pytest.mark.parametrize("drift", ["html", "body_text", "journal"])
def test_guest_rechecks_observation_and_intent_before_reporting_confirmation(tmp_path, drift):
    context = guest_context(tmp_path)

    class DriftingPage(ObservedPage):
        def read_only_snapshot(self):
            if self.reads == 1:
                if drift == "journal":
                    entry = json.loads(context["journal_path"].read_text(encoding="utf-8"))
                    record_page_action(context["journal_path"], action="submit", evidence=entry["evidence"])
                else:
                    self.snapshot[drift] = "No longer the confirmed page"
            return super().read_only_snapshot()

    result = reconcile(context, DriftingPage())
    assert result["portal_confirmed"] is False
    assert result["replay_allowed"] is False


@pytest.mark.parametrize("extra_html,extra_body", [
    ('<p>Your application could not be submitted.</p>', "Your application could not be submitted."),
    ('<p>Your application has not been received.</p>', "Your application has not been received."),
    ('<p>Please verify you are human.</p>', "Please verify you are human."),
    ('<input required name="first_name"><button type="submit">Submit application</button>', "Submit application"),
])
def test_guest_conflicting_failure_or_visible_gate_is_not_success(tmp_path, extra_html, extra_body):
    context = guest_context(tmp_path)
    html = f"<html><body><h1>{SUCCESS}</h1>{extra_html}</body></html>"
    body = SUCCESS + "\n" + extra_body
    context["transition"]["after_html_sha256"] = digest(html)
    context["transition"]["after_body_text_sha256"] = digest(body)
    result = reconcile(context, ObservedPage(html=html, body_text=body))
    assert result["portal_confirmed"] is False


@pytest.mark.parametrize("error", [RuntimeError, ValueError, OSError])
def test_guest_observation_errors_are_sanitized_inspection_only(tmp_path, error):
    class FailedPage(ObservedPage):
        def read_only_snapshot(self):
            raise error("<html>private-observation-sentinel</html>")

    result = reconcile(guest_context(tmp_path), FailedPage())
    assert result["portal_confirmed"] is False
    assert result["replay_allowed"] is False
    assert "private-observation-sentinel" not in json.dumps(result)


@pytest.mark.parametrize("attribute", ["", " aria-hidden"])
def test_guest_success_ignores_dormant_hidden_captcha_plumbing(tmp_path, attribute):
    context = guest_context(tmp_path)
    html = HTML.replace("<h1>", f"<h1{attribute}>").replace("</body>", '<input type="hidden" required name="g-recaptcha-response"><script>captcha()</script></body>')
    context["transition"]["after_html_sha256"] = digest(html)
    result = reconcile(context, ObservedPage(html=html))
    assert result["portal_confirmed"] is True


@pytest.mark.parametrize("success,interrupted", [(True, False), (True, True), (False, True)])
def test_canonical_one_shot_guest_integration_never_replays(tmp_path, success, interrupted):
    """Regression proof through real local authorization/journal, synthetic page."""
    import one_shot_submit
    from submission_authorization import SubmissionAuthorizationStore

    context = guest_context(tmp_path)
    context["journal_path"].unlink()  # The canonical runner, not this fixture, must journal.
    context.pop("transition")
    review = context["review_evidence"]
    store = SubmissionAuthorizationStore(tmp_path / "synthetic-authorization.sqlite3")
    issued = store.issue(job_id=context["job_id"], review_evidence=review, actor="synthetic-test",
                         issued_at="2026-09-05T12:00:00+00:00", expires_at="2026-09-05T12:05:00+00:00")

    class SyntheticOneShotPage(ObservedPage):
        def __init__(self):
            super().__init__(url=ORIGIN, html="<html><body>Synthetic original form</body></html>", body_text="Synthetic original form")
            self.snapshot.update(identity=IDENTITY, gates=[])
            self.click_count = 0

        def inspect_submit_control(self, selector):
            return {"selector": selector, "target_id": TARGET, "url": self.snapshot["url"],
                    "visible": True, "enabled": True, "unique": True, "role": "button"}

        def click_submit_once(self, selector):
            assert self.click_count == 0
            intent = json.loads(context["journal_path"].read_text(encoding="utf-8"))["evidence"]
            assert intent["status"] == "intent_recorded" and intent["verified"] is False
            before = self.read_only_snapshot()
            self.click_count += 1
            html = HTML if success else "<html><body>Processing application</body></html>"
            text = SUCCESS if success else "Processing application"
            self.snapshot.update(url=THANKYOU, html=html, body_text=text)
            after = self.read_only_snapshot()
            context["transition"] = {
                "source": "one_shot_same_tab_observation", "trusted": True,
                "target_id": TARGET, "from_url": before["url"], "to_url": after["url"],
                "intent_sha256": canonical_digest(intent), "before_html_sha256": digest(before["html"]),
                "after_html_sha256": digest(after["html"]), "after_body_text_sha256": digest(after["body_text"]),
            }
            if interrupted:
                raise one_shot_submit.SubmitInterrupted("synthetic connection interruption")

        def inspect_confirmation(self):
            return {"confirmed": reconcile(context, self)["portal_confirmed"]}

    page = SyntheticOneShotPage()
    result = one_shot_submit.execute_one_shot_submit(
        authorization_store=store, token=issued["token"], page=page, journal_path=context["journal_path"],
        job_id=context["job_id"], target_id=TARGET, expected_url=ORIGIN, requisition=IDENTITY["requisition"],
        review_evidence_sha256=review["review_evidence_sha256"], actor="synthetic-test",
        now="2026-09-05T12:01:00+00:00", submit_selector="button[type=submit]",
    )
    assert result["status"] == ("confirmation_observed" if success else "blocked")
    assert result["authorization_consumed"] is True
    assert result["replay_allowed"] is False
    assert page.click_count == 1
    journal_before = context["journal_path"].read_bytes()
    assert reconcile(context, page)["portal_confirmed"] is success
    assert context["journal_path"].read_bytes() == journal_before
    assert page.click_count == 1
    with pytest.raises(PermissionError, match="replayed"):
        store.consume(token=issued["token"], current_binding=issued["binding"], actor="synthetic-test", now="2026-09-05T12:01:01+00:00")
