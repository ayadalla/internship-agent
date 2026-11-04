from __future__ import annotations

import hashlib
import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


PAGE_URL = "https://job-boards.greenhouse.io/fixture/REQ-123"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_live_inputs(tmp_path: Path) -> tuple[Path, Path, dict]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    resume = runtime / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nsanitized live CLI resume")
    profile = runtime / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "name": {"first": "Fixture", "last": "Person"},
                "contact": {"email": "fixture@example.test", "phone": "+1-555-0100"},
                "work_authorization": "Yes", "requires_sponsorship": "No",
                "resume": {
                    "primary": str(resume),
                    "required_application_filename": "Resume.pdf",
                    "do_not_use_for_applications": [],
                },
            }
        )
    , encoding="utf-8")
    answers = runtime / "approved-answers.json"
    answers.write_text(
        json.dumps(
            {
                "first_name": "Fixture",
                "last_name": "Person",
                "email": "fixture@example.test",
                "phone": "+1-555-0100",
                "resume": str(resume),
                "work_authorization": "Yes",
                "sponsorship": "No",
            }
        )
    , encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "mode": "sanitized_local",
        "job_id": 41,
        "queue_id": "queue-41",
        "target": {"id": "target-abc", "url": PAGE_URL},
        "identity": {
            "company": "Sanitized Example",
            "role": "Software Engineer Intern",
            "requisition": "REQ-123",
            "platform": "greenhouse",
            "tenant": "fixture",
        },
        "profile": {"path": str(profile), "sha256": _sha256(profile), "verified": True},
        "resume": {
            "path": str(resume),
            "basename": "Resume.pdf",
            "content_type": "application/pdf",
            "sha256": _sha256(resume),
            "verified": True,
        },
        "manual_gate": {
            "gates": [],
            "maango": False,
            "maango_approved": False,
            "verified": True,
        },
        "runtime_paths": {
            "preparation": str(runtime / "preparation.json"),
            "review": str(runtime / "review.json"),
            "authorization_db": str(runtime / "authorization.sqlite3"),
            "authorization_handoff": str(runtime / "authorization.handoff"),
            "submit_journal": str(runtime / "submit.jsonl"),
            "confirmation": str(runtime / "confirmation.json"),
            "transaction_db": str(runtime / "transactions.sqlite3"),
            "status": str(runtime / "status.json"),
        },
    }
    manifest_path = runtime / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, answers, manifest


def test_canary_preserves_only_an_exact_existing_resume_slot():
    import production_operator

    class Page:
        def read_uploaded_filename(self, selector):
            assert selector == "#resume"
            return "Resume.pdf"

    selectors = production_operator._canary_selectors_for_actions(
        page=Page(),
        actions=[
            {"field": "first_name", "operation": "replace_text", "selector": "#first_name"},
            {"field": "resume", "operation": "cdp_upload", "selector": "#resume"},
        ],
        expected_resume_basename="Resume.pdf",
    )

    assert selectors == ["#first_name"]


def test_canary_requires_a_resume_control_when_existing_filename_does_not_match():
    import production_operator

    class Page:
        def read_uploaded_filename(self, selector):
            return "Old Resume.pdf"

    selectors = production_operator._canary_selectors_for_actions(
        page=Page(),
        actions=[{"field": "resume", "operation": "cdp_upload", "selector": "#resume"}],
        expected_resume_basename="Resume.pdf",
    )

    assert selectors == ["#resume"]


def test_live_prepare_cli_exact_binds_runs_gates_uploads_profile_resume_and_persists_sanitized_evidence(
    tmp_path, capsys
):
    import production_operator

    manifest_path, answers_path, manifest = _write_live_inputs(tmp_path)
    fixture_html = (ROOT / "fixtures" / "local_operator_e2e.html").read_text(encoding="utf-8")

    class Page:
        target_id = "target-abc"

        def __init__(self):
            self.values: dict[str, object] = {}
            self.uploaded_path = ""
            self.closed = False
            self.canary_selectors: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def read_only_snapshot(self):
            return {
                "target_id": self.target_id,
                "url": PAGE_URL,
                "html": fixture_html,
                "read_only": True,
            }

        def inspect_safety_surface(self, selectors):
            self.canary_selectors = list(selectors)
            return {
                "retina_scale": 2.0,
                "control_visible": True,
                "overlay_present": False,
                "native_window_detected": False,
            }

        def replace_text(self, selector, value):
            self.values[selector] = value

        def read_value(self, selector):
            return self.values[selector]

        def select_option(self, selector, value):
            self.values[selector] = value

        def read_selected_option(self, selector):
            return self.values[selector]

        def cdp_upload(self, selector, path):
            self.uploaded_path = path
            self.values[selector] = Path(path).name

        def read_uploaded_filename(self, selector):
            return self.values.get(selector, "")

    page = Page()
    bound_targets: list[tuple[str, str]] = []

    class Transport:
        def __init__(self, base_url):
            self.base_url = base_url

        def bind_mutable_page_target(self, target_id):
            bound_targets.append((self.base_url, target_id))
            return page

    result = production_operator.main(
        [
            "live",
            "prepare",
            "--manifest",
            str(manifest_path),
            "--approved-answers",
            str(answers_path),
            "--step",
            "application",
            "--cdp-base-url",
            "http://127.0.0.1:9222",
        ],
        live_transport_factory=Transport,
        live_health_probe=lambda base_url: {
            "status": "ready",
            "base_url": base_url,
            "recoverable": False,
        },
        live_coverage=lambda **kwargs: {
            "known": [{"question_key": "first_name", "source": "profile"}],
            "company_specific": [],
            "optional_skip": [],
            "human_required": [],
        },
    )

    assert result == 0
    assert bound_targets == [("http://127.0.0.1:9222", "target-abc")]
    assert page.closed is True
    assert set(page.canary_selectors) == {
        "#first_name",
        "#last_name",
        "#email",
        "#phone",
        "#resume",
        "#authorization",
        "#sponsorship",
    }
    assert page.uploaded_path == manifest["resume"]["path"]

    persisted_path = Path(manifest["runtime_paths"]["preparation"])
    persisted_text = persisted_path.read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    assert json.loads(capsys.readouterr().out) == persisted
    assert persisted["status"] == "prepared"
    assert persisted["submission_enabled"] is False
    assert persisted["review_ready"] is True
    assert persisted["manifest_binding"] == {
        "schema_version": 1,
        "mode": "sanitized_local",
        "job_id": 41,
        "queue_id": "queue-41",
        "tenant": "fixture",
    }
    assert persisted["health"] == {
        "browser_ready": True,
        "canary_passed": True,
        "exact_target_bound": True,
    }
    assert persisted["resume"] == {"basename": "Resume.pdf", "verified": True}
    assert len(persisted["applied_answers"]["field_evidence"]) == 7
    for sensitive in (
        "Fixture Person",
        "fixture@example.test",
        "+1-555-0100",
        str(tmp_path),
    ):
        assert sensitive not in persisted_text


def test_live_review_cli_reads_exact_server_review_and_persists_only_authority_evidence(
    tmp_path, capsys
):
    import production_operator

    manifest_path, answers_path, manifest = _write_live_inputs(tmp_path)
    fixture_html = (ROOT / "fixtures" / "local_operator_e2e.html").read_text(encoding="utf-8")
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    preparation = {
        "target_id": "target-abc",
        "page_url": PAGE_URL,
        "identity": {
            key: manifest["identity"][key]
            for key in ("company", "role", "requisition")
        },
        "platform": "greenhouse",
        "submission_enabled": False,
        "review_ready": True,
        "answer_coverage": {"human_required": []},
        "applied_answers": {
            "verified": True,
            "field_evidence": [
                {"selector": selector, "verified": True}
                for selector in (
                    "#first_name",
                    "#last_name",
                    "#email",
                    "#phone",
                    "#resume",
                    "#authorization",
                    "#sponsorship",
                )
            ],
        },
        "evidence": {
            "sanitized": True,
            "target_bound": True,
            "answer_values_persisted": False,
        },
    }
    Path(manifest["runtime_paths"]["preparation"]).write_text(json.dumps(preparation), encoding="utf-8")

    class ReviewPage:
        target_id = "target-abc"

        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def read_only_snapshot(self):
            return {
                "target_id": self.target_id,
                "url": PAGE_URL,
                "html": fixture_html,
                "read_only": True,
            }

        def read_server_review(self):
            return {
                "source": "server_saved_review", "server_saved": True,
                "target_id": self.target_id,
                "page_url": PAGE_URL,
                "identity": {
                    key: manifest["identity"][key]
                    for key in ("company", "role", "requisition")
                },
                "fields": {
                    "#first_name": answers["first_name"],
                    "#last_name": answers["last_name"],
                    "#email": answers["email"],
                    "#phone": answers["phone"],
                    "#authorization": answers["work_authorization"],
                    "#sponsorship": answers["sponsorship"],
                },
                "resume": {
                    "basename": "Resume.pdf",
                    "sha256": manifest["resume"]["sha256"],
                },
                "parser_repairs": [],
                "questions": [
                    {
                        "id": "work_authorization",
                        "required": True,
                        "answered": True,
                        "verified": True,
                    }
                ],
            }

    page = ReviewPage()

    class Transport:
        def __init__(self, base_url):
            assert base_url == "http://127.0.0.1:9222"

        def bind_mutable_page_target(self, target_id):
            assert target_id == "target-abc"
            return page

    result = production_operator.main(
        [
            "live",
            "review",
            "--manifest",
            str(manifest_path),
            "--approved-answers",
            str(answers_path),
            "--step",
            "application",
            "--required-question",
            "work_authorization",
        ],
        live_transport_factory=Transport,
        live_health_probe=lambda base_url: {"status": "ready", "base_url": base_url},
    )

    assert result == 0
    assert page.closed is True
    persisted_text = Path(manifest["runtime_paths"]["review"]).read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    review = persisted["review"]
    assert persisted["status"] == "reviewed"
    assert review["review_authoritative"] is True
    assert review["submission_authorized"] is False
    assert review["human_required"] == []
    assert len(review["review_evidence_sha256"]) == 64

    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "status": "reviewed",
        "review_authoritative": True,
        "review_evidence_sha256": review["review_evidence_sha256"],
        "verified_fields": sorted([
            "#first_name", "#last_name", "#email", "#phone", "#authorization", "#sponsorship",
        ]),
        "blockers": [],
        "job_identity": {
            "job_id": 41,
            "queue_id": "queue-41",
            "target_id": "target-abc",
            "page_url": PAGE_URL,
            **manifest["identity"],
        },
    }
    for sensitive in (
        "Fixture Person",
        "fixture@example.test",
        "+1-555-0100",
        str(tmp_path),
        manifest["resume"]["sha256"],
    ):
        assert sensitive not in persisted_text
        assert sensitive not in json.dumps(summary)


def _write_authoritative_review(manifest: dict) -> dict:
    """Offline fixture uses the real reconciler, not an invented boolean hash."""
    import production_operator as op
    import review_reconciler
    import tenant_field_maps
    profile = json.loads(Path(manifest['profile']['path']).read_text(encoding="utf-8"))
    fields = {'#first_name':'Fixture', '#last_name':'Person', '#email':'fixture@example.test',
              '#phone':'+1-555-0100', '#authorization':'Yes', '#sponsorship':'No'}
    identity = {key:manifest['identity'][key] for key in ('company','role','requisition')}
    preparation_path = Path(manifest['runtime_paths']['preparation'])
    preparation = json.loads(preparation_path.read_text(encoding="utf-8")) if preparation_path.exists() else {
        'target_id':manifest['target']['id'], 'page_url':manifest['target']['url'], 'identity':identity,
        'submission_enabled':False, 'review_ready':True, 'answer_coverage':{'human_required':[]},
        'applied_answers':{'verified':True,'field_evidence':[]},
        'evidence':{'sanitized':True,'target_bound':True,'answer_values_persisted':False}}
    raw={'source':'server_saved_review','server_saved':True,
         'target_id':manifest['target']['id'],'page_url':manifest['target']['url'],'identity':identity,
         'fields':fields, 'resume':{'basename':'Resume.pdf','sha256':manifest['resume']['sha256']},
         'parser_repairs':[],'questions':[{'id':'work_authorization','required':True,'answered':True,'verified':True}]}
    mapping=tenant_field_maps.resolve_field_map(page_url=manifest['target']['url'],platform='greenhouse')
    expected=op._independent_review_fields(profile,mapping,'application',raw)
    review=review_reconciler.reconcile_review(preparation_evidence=preparation,server_review=raw,
        expected_target={'target_id':manifest['target']['id'],'page_url':manifest['target']['url'],**identity},
        profile_fields=expected,resume_preflight={k:manifest['resume'][k] for k in ('basename','content_type','sha256','verified')},required_parser_repairs=[],
        required_question_ids=['work_authorization'],question_fields={k:v['selector'] for k,v in mapping['steps']['application']['controls'].items()})
    assert review['review_authoritative'] is True, review
    wrapper={'status':'reviewed','job_identity':op._manifest_job_identity(manifest),'review':review}
    Path(manifest['runtime_paths']['review']).write_text(json.dumps(wrapper), encoding="utf-8")
    return wrapper


def test_live_authorize_cli_requires_explicit_hash_and_delivers_token_only_to_protected_handoff(
    tmp_path, capsys
):
    import production_operator

    manifest_path, _, manifest = _write_live_inputs(tmp_path)
    wrapper = _write_authoritative_review(manifest)
    review_hash = wrapper["review"]["review_evidence_sha256"]
    now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)

    result = production_operator.main(
        [
            "live",
            "authorize",
            "--manifest",
            str(manifest_path),
            "--actor",
            "sanitized-local-operator",
            "--approve-review-hash",
            review_hash,
            "--expires-in-seconds",
            "300",
        ],
        live_clock=lambda: now,
    )

    assert result == 0
    handoff_path = Path(manifest["runtime_paths"]["authorization_handoff"])
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert stat.S_IMODE(handoff_path.stat().st_mode) == 0o600
    assert handoff["schema_version"] == 1
    assert handoff["actor"] == "sanitized-local-operator"
    assert handoff["review_evidence_sha256"] == review_hash
    assert handoff["single_use"] is True
    assert isinstance(handoff["token"], str) and len(handoff["token"]) > 32

    stdout_text = capsys.readouterr().out
    assert handoff["token"] not in stdout_text
    assert str(tmp_path) not in stdout_text
    assert json.loads(stdout_text) == {
        "status": "authorized",
        "authorization_issued": True,
        "single_use": True,
        "actor": "sanitized-local-operator",
        "expires_at": "2026-08-27T10:05:00+00:00",
        "review_evidence_sha256": review_hash,
        "token_delivery": "protected_runtime_handoff",
        "job_identity": wrapper["job_identity"],
    }
    database_bytes = Path(manifest["runtime_paths"]["authorization_db"]).read_bytes()
    assert handoff["token"].encode() not in database_bytes
    assert handoff["token"] not in Path(manifest["runtime_paths"]["review"]).read_text(encoding="utf-8")


def test_live_authorize_requires_separate_maango_approval_when_manifest_is_maango(
    tmp_path, capsys
):
    import production_operator

    manifest_path, _, manifest = _write_live_inputs(tmp_path)
    manifest["manual_gate"].update({"maango": True, "maango_approved": True})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    wrapper = _write_authoritative_review(manifest)

    result = production_operator.main(
        [
            "live",
            "authorize",
            "--manifest",
            str(manifest_path),
            "--actor",
            "sanitized-local-operator",
            "--approve-review-hash",
            wrapper["review"]["review_evidence_sha256"],
            "--expires-in-seconds",
            "300",
        ],
        live_clock=lambda: datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
    )

    assert result == 2
    assert "explicit MAANGO approval is required" in capsys.readouterr().out
    assert not Path(manifest["runtime_paths"]["authorization_handoff"]).exists()


def test_live_submit_reconciles_fresh_review_journals_before_one_click_and_denies_replay(
    tmp_path, capsys
):
    import production_operator

    manifest_path, answers_path, manifest = _write_live_inputs(tmp_path)
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    preparation = {
        "target_id": "target-abc",
        "page_url": PAGE_URL,
        "identity": {
            key: manifest["identity"][key]
            for key in ("company", "role", "requisition")
        },
        "platform": "greenhouse",
        "submission_enabled": False,
        "review_ready": True,
        "answer_coverage": {"human_required": []},
        "applied_answers": {
            "verified": True,
            "field_evidence": [
                {"selector": selector, "verified": True}
                for selector in (
                    "#first_name",
                    "#last_name",
                    "#email",
                    "#phone",
                    "#resume",
                    "#authorization",
                    "#sponsorship",
                )
            ],
        },
        "evidence": {
            "sanitized": True,
            "target_bound": True,
            "answer_values_persisted": False,
        },
    }
    Path(manifest["runtime_paths"]["preparation"]).write_text(json.dumps(preparation), encoding="utf-8")
    wrapper = _write_authoritative_review(manifest)
    now = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    assert production_operator.main(
        [
            "live",
            "authorize",
            "--manifest",
            str(manifest_path),
            "--actor",
            "sanitized-local-operator",
            "--approve-review-hash",
            wrapper["review"]["review_evidence_sha256"],
            "--expires-in-seconds",
            "300",
        ],
        live_clock=lambda: now,
    ) == 0
    capsys.readouterr()

    fixture_html = (ROOT / "fixtures" / "local_operator_e2e.html").read_text(encoding="utf-8")

    class SubmitPage:
        target_id = "target-abc"

        def __init__(self):
            self.clicks = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read_only_snapshot(self):
            return {
                "target_id": self.target_id,
                "url": PAGE_URL,
                "html": fixture_html,
                "read_only": True,
                "identity": {
                    key: manifest["identity"][key]
                    for key in ("company", "role", "requisition")
                },
                "gates": [],
                "maango": False,
            }

        def read_server_review(self):
            return {
                "source":"server_saved_review", "server_saved":True,
                "target_id": self.target_id,
                "page_url": PAGE_URL,
                "identity": self.read_only_snapshot()["identity"],
                "fields": {
                    "#first_name": answers["first_name"],
                    "#last_name": answers["last_name"],
                    "#email": answers["email"],
                    "#phone": answers["phone"],
                    "#authorization": answers["work_authorization"],
                    "#sponsorship": answers["sponsorship"],
                },
                "resume": {
                    "basename": "Resume.pdf",
                    "sha256": manifest["resume"]["sha256"],
                },
                "parser_repairs": [],
                "questions": [
                    {
                        "id": "work_authorization",
                        "required": True,
                        "answered": True,
                        "verified": True,
                    }
                ],
            }

        def inspect_submit_control(self, selector):
            return {
                "selector": selector,
                "target_id": self.target_id,
                "url": PAGE_URL,
                "visible": True,
                "enabled": True,
                "unique": True,
                "role": "button",
            }

        def click_submit_once(self, selector):
            assert selector == "#submit"
            journal = Path(manifest["runtime_paths"]["submit_journal"])
            assert journal.exists()
            assert json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])["evidence"][
                "status"
            ] == "intent_recorded"
            self.clicks += 1

        def inspect_confirmation(self):
            return {"confirmed": self.clicks == 1, "state": "submitted"}

    page = SubmitPage()

    class Transport:
        def __init__(self, base_url):
            assert base_url == "http://127.0.0.1:9222"

        def bind_mutable_page_target(self, target_id):
            assert target_id == "target-abc"
            return page

    argv = [
        "live",
        "submit",
        "--manifest",
        str(manifest_path),
        "--approved-answers",
        str(answers_path),
        "--step",
        "application",
        "--required-question",
        "work_authorization",
        "--actor",
        "sanitized-local-operator",
    ]
    result = production_operator.main(
        argv,
        live_transport_factory=Transport,
        live_health_probe=lambda base_url: {"status": "ready", "base_url": base_url},
        live_clock=lambda: now,
    )

    assert result == 0
    assert page.clicks == 1
    assert not Path(manifest["runtime_paths"]["authorization_handoff"]).exists()
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "status": "confirmation_observed",
        "stage": "confirmation_inspection",
        "authorization_consumed": True,
        "one_shot": True,
        "replay_allowed": False,
        "confirmation_reconciled": False,
        "next_action": "live confirmation",
        "job_identity": wrapper["job_identity"],
    }

    assert production_operator.main(
        argv,
        live_transport_factory=Transport,
        live_health_probe=lambda base_url: {"status": "ready", "base_url": base_url},
        live_clock=lambda: now,
    ) == 2
    assert page.clicks == 1
    assert "authorization handoff is unavailable" in capsys.readouterr().out


def test_live_confirmation_cli_persists_sanitized_exact_candidate_home_reconciliation(
    tmp_path, capsys
):
    import production_operator

    manifest_path, _, manifest = _write_live_inputs(tmp_path)
    Path(manifest["runtime_paths"]["submit_journal"]).write_text(
        json.dumps(
            {
                "action": "submit",
                "evidence": {
                    "status": "confirmation_observed",
                    "verified": True,
                    "job_id": 41,
                    "target_id": "target-abc",
                    "page_url": PAGE_URL,
                    "requisition": "REQ-123",
                },
            }
        )
        + "\n"
    , encoding="utf-8")
    confirmation_html = (ROOT / "fixtures" / "greenhouse_confirmation.html").read_text(encoding="utf-8")

    class Page:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read_only_snapshot(self):
            return {
                "target_id": "target-abc",
                "url": PAGE_URL,
                "html": confirmation_html,
                "read_only": True,
            }

        def read_greenhouse_candidate_applications(self):
            return [
                {
                    "platform": "greenhouse",
                    "company": "Sanitized Example",
                    "role": "Software Engineer Intern",
                    "requisition": "REQ-123",
                    "state": "submitted",
                    "submitted": True,
                }
            ]

    class Transport:
        def __init__(self, base_url):
            assert base_url == "http://127.0.0.1:9222"

        def bind_mutable_page_target(self, target_id):
            assert target_id == "target-abc"
            return Page()

    result = production_operator.main(
        ["live", "confirmation", "--manifest", str(manifest_path)],
        live_transport_factory=Transport,
        live_health_probe=lambda base_url: {"status": "ready", "base_url": base_url},
    )

    assert result == 0
    persisted_text = Path(manifest["runtime_paths"]["confirmation"]).read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    assert persisted["status"] == "portal_confirmed"
    assert persisted["portal"]["portal_confirmed"] is True
    assert persisted["portal"]["safe_for_post_submit"] is True
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "status": "portal_confirmed",
        "portal_confirmed": True,
        "safe_for_post_submit": True,
        "matched_application_count": 1,
        "human_required": [],
        "reader": {"platform": "greenhouse", "tenant": "fixture", "verified": True},
        "job_identity": persisted["job_identity"],
    }
    assert "<html" not in persisted_text.casefold()
    assert str(tmp_path) not in persisted_text


def test_live_delivery_cli_uses_injected_local_adapters_in_order_and_is_idempotent(
    tmp_path, capsys
):
    import production_operator

    manifest_path, _, manifest = _write_live_inputs(tmp_path)
    portal = {
        "portal_confirmed": True,
        "safe_for_post_submit": True,
        "platform": "greenhouse",
        "identity": {
            key: manifest["identity"][key]
            for key in ("company", "role", "requisition")
        },
        "confirmation": {
            "url": PAGE_URL,
            "reference_id": None,
            "submitted": True,
            "text_sha256": "a" * 64,
        },
        "portal_readback": {
            "matched_application_count": 1,
            "state": "submitted",
            "submitted": True,
            "verified": True,
        },
        "human_required": [],
        "evidence": {"sanitized": True, "two_source_reconciliation": True},
        "reader": {"platform": "greenhouse", "tenant": "fixture", "verified": True},
    }
    Path(manifest["runtime_paths"]["confirmation"]).write_text(
        json.dumps(
            {
                "status": "portal_confirmed",
                "job_identity": {
                    "job_id": manifest["job_id"],
                    "queue_id": manifest["queue_id"],
                    "target_id": manifest["target"]["id"],
                    "page_url": manifest["target"]["url"],
                    **manifest["identity"],
                },
                "portal": portal,
            }
        )
    , encoding="utf-8")
    events = []

    class Tracker:
        def __init__(self):
            self.record = None
            self.appends = 0

        def append(self, **kwargs):
            events.append("tracker.append")
            self.appends += 1
            self.record = kwargs

        def read_back(self, *, transaction_id):
            events.append("tracker.read_back")
            if self.record is None:
                return None
            return {
                "verified": True,
                "transaction_id": transaction_id,
                "payload_sha256": self.record["payload_sha256"],
                "receipt_id": "local-row",
            }

    class Discord:
        def __init__(self):
            self.record = None
            self.sends = 0

        def send(self, **kwargs):
            events.append("discord.send")
            self.sends += 1
            self.record = kwargs

        def read_back(self, *, transaction_id):
            events.append("discord.read_back")
            if self.record is None:
                return None
            return {
                "verified": True,
                "transaction_id": transaction_id,
                "message_sha256": self.record["message_sha256"],
                "receipt_id": "local-message",
            }

    tracker_adapter = Tracker()
    discord_adapter = Discord()
    argv = [
        "live",
        "deliver",
        "--sync-tracker",
        "--manifest",
        str(manifest_path),
        "--submitted-date",
        "2026-08-27",
    ]
    assert production_operator.main(
        argv,
        live_tracker_adapter=tracker_adapter,
        live_discord_adapter=discord_adapter,
    ) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "complete"
    assert first["tracker"]["readback_verified"] is True
    assert first["discord"]["readback_verified"] is True
    assert events == [
        "tracker.read_back",
        "tracker.append",
        "tracker.read_back",
        "discord.read_back",
        "discord.send",
        "discord.read_back",
    ]

    assert production_operator.main(
        argv,
        live_tracker_adapter=tracker_adapter,
        live_discord_adapter=discord_adapter,
    ) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert tracker_adapter.appends == 1
    assert discord_adapter.sends == 1


def test_live_status_and_resume_turn_uncertain_submit_into_confirmation_inspection_only(
    tmp_path, capsys
):
    import production_operator

    manifest_path, _, manifest = _write_live_inputs(tmp_path)
    _write_authoritative_review(manifest)
    Path(manifest["runtime_paths"]["preparation"]).write_text(
        json.dumps({"status": "prepared"})
    , encoding="utf-8")
    Path(manifest["runtime_paths"]["submit_journal"]).write_text(
        json.dumps(
            {
                "action": "submit",
                "evidence": {
                    "status": "intent_recorded",
                    "verified": False,
                    "job_id": 41,
                    "target_id": "target-abc",
                    "page_url": PAGE_URL,
                    "requisition": "REQ-123",
                },
            }
        )
        + "\n"
    , encoding="utf-8")

    assert production_operator.main(
        ["live", "status", "--manifest", str(manifest_path)]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["stages"] == {
        "preparation": "complete",
        "review": "complete",
        "authorization": "consumed_or_unavailable",
        "submit": "uncertain",
        "confirmation": "not_started",
        "tracker": "not_started",
        "discord": "not_started",
    }
    assert status["next_action"] == "inspect_confirmation_without_replay"
    assert status["submit_replay_allowed"] is False
    assert json.loads(Path(manifest["runtime_paths"]["status"]).read_text(encoding="utf-8")) == status

    confirmation_html = (ROOT / "fixtures" / "greenhouse_confirmation.html").read_text(encoding="utf-8")

    class InspectionOnlyPage:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read_only_snapshot(self):
            return {
                "target_id": "target-abc",
                "url": PAGE_URL,
                "html": confirmation_html,
                "read_only": True,
            }

        def read_greenhouse_candidate_applications(self):
            return [
                {
                    "platform": "greenhouse",
                    "company": "Sanitized Example",
                    "role": "Software Engineer Intern",
                    "requisition": "REQ-123",
                    "state": "submitted",
                    "submitted": True,
                }
            ]

        def click_submit_once(self, selector):
            raise AssertionError("resume must never replay submit")

    class Transport:
        def __init__(self, base_url):
            assert base_url == "http://127.0.0.1:9222"

        def bind_mutable_page_target(self, target_id):
            assert target_id == "target-abc"
            return InspectionOnlyPage()

    assert production_operator.main(
        ["live", "resume", "--manifest", str(manifest_path)],
        live_transport_factory=Transport,
        live_health_probe=lambda base_url: {"status": "ready", "base_url": base_url},
    ) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["resume_action"] == "confirmation_inspection_without_replay"
    assert resumed["portal_confirmed"] is True
    assert resumed["submit_replay_allowed"] is False
