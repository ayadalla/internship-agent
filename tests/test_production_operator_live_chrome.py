from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


VIRTUAL_URL = "https://job-boards.greenhouse.io/fixture/REQ-123"


def _manifest(tmp_path: Path, *, target_id: str) -> tuple[Path, Path, dict]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    resume = runtime / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nsanitzed complete CLI Chrome resume")
    profile = runtime / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "resume": {
                    "primary": str(resume),
                    "required_application_filename": "Resume.pdf",
                    "do_not_use_for_applications": [],
                }
            }
        )
    , encoding="utf-8")
    answers = runtime / "approved.json"
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
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "mode": "sanitized_local",
        "job_id": 91,
        "queue_id": "queue-91",
        "target": {"id": target_id, "url": VIRTUAL_URL},
        "identity": {
            "company": "Sanitized Example",
            "role": "Software Engineer Intern",
            "requisition": "REQ-123",
            "platform": "greenhouse",
            "tenant": "fixture",
        },
        "profile": {"path": str(profile), "sha256": digest(profile), "verified": True},
        "resume": {
            "path": str(resume),
            "basename": "Resume.pdf",
            "content_type": "application/pdf",
            "sha256": digest(resume),
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
    path = runtime / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path, answers, manifest


def test_complete_live_cli_sequence_in_real_local_chrome_with_failure_matrix(
    tmp_path, capsys
):
    import local_cdp_operator
    import production_operator

    class VirtualPage:
        def __init__(self, page):
            self.page = page
            self.target_id = page.target_id

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.page.__exit__(*args)

        def read_only_snapshot(self):
            return {**self.page.read_only_snapshot(), "url": VIRTUAL_URL}

        def inspect_safety_surface(self, selectors):
            return self.page.inspect_safety_surface(selectors)

        def replace_text(self, selector, value):
            self.page.replace_text(selector, value)

        def read_value(self, selector):
            return self.page.read_value(selector)

        def select_option(self, selector, value):
            self.page.select_option(selector, value)

        def read_selected_option(self, selector):
            return self.page.read_selected_option(selector)

        def cdp_upload(self, selector, path):
            self.page.cdp_upload(selector, path)

        def read_uploaded_filename(self, selector):
            return self.page.read_uploaded_filename(selector)

        def read_server_review(self):
            return {**self.page.read_server_review(), "page_url": VIRTUAL_URL}

        def inspect_submit_control(self, selector):
            return {**self.page.inspect_submit_control(selector), "url": VIRTUAL_URL}

        def click_submit_once(self, selector):
            self.page.click_submit_once(selector)

        def inspect_confirmation(self):
            return self.page.inspect_confirmation()

        def read_candidate_applications(self):
            return self.page.read_candidate_applications()

    with local_cdp_operator.LocalChromeFixtureSession(
        fixture_path=ROOT / "fixtures" / "local_operator_e2e.html",
        chrome_path=local_cdp_operator.find_local_chrome_for_testing(),
        runtime_dir=tmp_path / "chrome",
    ) as session:
        manifest_path, answers_path, manifest = _manifest(
            tmp_path, target_id=session.target_id
        )

        class Transport:
            def __init__(self, base_url):
                assert base_url == "http://127.0.0.1:9222"

            def bind_mutable_page_target(self, target_id):
                return VirtualPage(session.bind_exact_page(target_id))

        common = {
            "live_transport_factory": Transport,
            "live_health_probe": lambda base_url: {
                "status": "ready",
                "base_url": base_url,
            },
            "live_coverage": lambda **kwargs: {
                "known": [],
                "company_specific": [],
                "optional_skip": [],
                "human_required": [],
            },
            "live_clock": lambda: datetime(
                2026, 8, 27, 10, 0, tzinfo=timezone.utc
            ),
        }
        prepare_argv = [
            "live",
            "prepare",
            "--manifest",
            str(manifest_path),
            "--approved-answers",
            str(answers_path),
            "--step",
            "application",
        ]

        with session.bind_exact_page(session.target_id) as page:
            page.toggle_overlay_for_test(True)
        assert production_operator.main(prepare_argv, **common) == 2
        assert not Path(manifest["runtime_paths"]["preparation"]).exists()
        assert "browser canary blocked" in capsys.readouterr().out
        with session.bind_exact_page(session.target_id) as page:
            page.toggle_overlay_for_test(False)

        assert production_operator.main(prepare_argv, **common) == 0
        capsys.readouterr()
        with session.bind_exact_page(session.target_id) as page:
            page.wait_for_resume_sha256(manifest["resume"]["sha256"])
            page.activate_review("#review")

        review_argv = [
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
        ]
        assert production_operator.main(review_argv, **common) == 0
        review_summary = json.loads(capsys.readouterr().out)

        assert production_operator.main(
            [
                "live",
                "authorize",
                "--manifest",
                str(manifest_path),
                "--actor",
                "sanitized-cli-operator",
                "--approve-review-hash",
                review_summary["review_evidence_sha256"],
                "--expires-in-seconds",
                "300",
            ],
            **common,
        ) == 0
        capsys.readouterr()

        submit_argv = [
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
            "sanitized-cli-operator",
        ]
        assert production_operator.main(submit_argv, **common) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "confirmation_observed"
        assert production_operator.main(submit_argv, **common) == 2
        assert "authorization handoff is unavailable" in capsys.readouterr().out

        assert production_operator.main(
            ["live", "confirmation", "--manifest", str(manifest_path)], **common
        ) == 0
        assert json.loads(capsys.readouterr().out)["portal_confirmed"] is True
        assert production_operator.main(
            [
                "live",
                "deliver",
                "--manifest",
                str(manifest_path),
                "--submitted-date",
                "2026-08-27",
            ],
            **common,
        ) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "complete"
        assert production_operator.main(
            ["live", "status", "--manifest", str(manifest_path)], **common
        ) == 0
        assert json.loads(capsys.readouterr().out)["next_action"] == "complete"

        with session.bind_exact_page(session.target_id) as page:
            assert page.read_submit_count() == 1


def test_normal_chrome_preflight_uses_read_only_exact_target_without_page_mutation(
    tmp_path, capsys
):
    import production_operator

    manifest_path, _, _ = _manifest(tmp_path, target_id="normal-target")
    fixture_html = (ROOT / "fixtures" / "local_operator_e2e.html").read_text(encoding="utf-8")
    calls = []

    class ReadOnlyPage:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def read_only_snapshot(self):
            calls.append("snapshot")
            return {
                "target_id": "normal-target",
                "url": VIRTUAL_URL,
                "html": fixture_html,
                "title": "Sanitized Local ATS Operator Fixture",
                "body_text": "Sanitized Example REQ-123 Software Engineer Intern",
                "read_only": True,
            }

    class ReadOnlyTransport:
        def __init__(self, base_url):
            assert base_url == "http://127.0.0.1:9222"

        def bind_page_target(self, target_id):
            calls.append(("bind", target_id))
            return ReadOnlyPage()

    result = production_operator.main(
        ["live", "preflight", "--manifest", str(manifest_path)],
        live_readonly_transport_factory=ReadOnlyTransport,
        live_health_probe=lambda base_url: {"status": "ready", "base_url": base_url},
    )

    assert result == 0
    assert calls == [("bind", "normal-target"), "snapshot", "snapshot", "closed"]
    assert json.loads(capsys.readouterr().out) == {
        "status": "ready",
        "exact_target_attached": True,
        "read_only": True,
        "content_unchanged": True,
        "identity_verified": True,
        "submission_enabled": False,
        "job_identity": {
            "job_id": 91,
            "queue_id": "queue-91",
            "target_id": "normal-target",
            "page_url": VIRTUAL_URL,
            "company": "Sanitized Example",
            "role": "Software Engineer Intern",
            "requisition": "REQ-123",
            "platform": "greenhouse",
            "tenant": "fixture",
        },
    }
