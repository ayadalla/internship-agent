from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeLivePage:
    target_id = "page-42"

    def read_only_snapshot(self) -> dict[str, object]:
        return {
            "target_id": "page-42",
            "url": "https://job-boards.greenhouse.io/example/jobs/123",
            "html": "<html>fixture</html>",
            "read_only": True,
        }


def test_prepare_live_job_binds_exact_target_verifies_identity_and_stays_non_submitting():
    import prepare_live_job

    result = prepare_live_job.prepare_live_job(
        page=FakeLivePage(),
        target_id="page-42",
        expected_url="https://job-boards.greenhouse.io/example/jobs/123",
        expected_identity={
            "company": "Example Inc",
            "role": "Software Engineer Intern",
            "requisition": "123",
        },
        profile={"name": "Kevin"},
        prepare=lambda **kwargs: {
            "platform": "greenhouse",
            "company": "Example Inc",
            "role": "Software Engineer Intern",
            "requisition": "123",
            "questions": [],
            "submission_enabled": False,
        },
        coverage=lambda **kwargs: {"known": [], "company_specific": [], "optional_skip": [], "human_required": []},
        approved_answers={"#first-name": "Kevin"},
        apply_known=lambda answers: {
            "action": "fill_known_page",
            "field_evidence": [{"selector": "#first-name", "expected": answers["#first-name"], "actual": "Kevin", "verified": True}],
            "verified": True,
        },
    )

    assert result == {
        "target_id": "page-42",
        "page_url": "https://job-boards.greenhouse.io/example/jobs/123",
        "identity": {"company": "Example Inc", "role": "Software Engineer Intern", "requisition": "123"},
        "platform": "greenhouse",
        "submission_enabled": False,
        "review_ready": True,
        "answer_coverage": {"known": [], "company_specific": [], "optional_skip": [], "human_required": []},
        "applied_answers": {
            "action": "fill_known_page",
            "field_evidence": [{"selector": "#first-name", "expected": "Kevin", "actual": "Kevin", "verified": True}],
            "verified": True,
        },
        "evidence": {"sanitized": True, "target_bound": True},
    }


def test_prepare_live_job_resolves_required_question_covered_by_verified_mapped_control():
    import prepare_live_job

    result = prepare_live_job.prepare_live_job(
        page=FakeLivePage(),
        target_id="page-42",
        expected_url="https://job-boards.greenhouse.io/example/jobs/123",
        expected_identity={
            "company": "Example Inc",
            "role": "Software Engineer Intern",
            "requisition": "123",
        },
        profile={},
        prepare=lambda **kwargs: {
            "platform": "greenhouse",
            "company": "Example Inc",
            "role": "Software Engineer Intern",
            "requisition": "123",
            "questions": [{"label": "question_37929970002[]", "required": True}],
            "submission_enabled": False,
        },
        coverage=lambda **kwargs: {
            "known": [],
            "company_specific": [],
            "optional_skip": [],
            "human_required": [{
                "question": "question_37929970002[]",
                "question_key": "unknown",
                "reason": "unknown_question",
            }],
        },
        approved_answers={"privacy_accept": True},
        apply_known=lambda answers: {
            "action": "apply_learned_step",
            "field_evidence": [{
                "field": "privacy_accept",
                "selector": "#question_37929970002\\[\\]_252235310002",
                "verified": answers["privacy_accept"] is True,
            }],
            "verified": True,
        },
    )

    assert result["review_ready"] is True
    assert result["answer_coverage"]["human_required"] == []
    assert result["answer_coverage"]["known"] == [{
        "question": "question_37929970002[]",
        "question_key": "privacy_accept",
        "source": "verified_approved_answer",
    }]


def test_prepare_live_job_rejects_target_or_identity_drift_before_handler_dispatch():
    import prepare_live_job

    with pytest.raises(prepare_live_job.LivePreparationError, match="target URL changed"):
        prepare_live_job.prepare_live_job(
            page=FakeLivePage(),
            target_id="page-42",
            expected_url="https://job-boards.greenhouse.io/example/jobs/other",
            expected_identity={"company": "Example Inc", "role": "Software Engineer Intern", "requisition": "123"},
            profile={},
            prepare=lambda **kwargs: pytest.fail("handler must not run"),
            coverage=lambda **kwargs: pytest.fail("coverage must not run"),
        )


def test_cli_fresh_binds_applies_explicit_answers_and_persists_sanitized_evidence(
    tmp_path, capsys
):
    import prepare_live_job

    page_url = "https://job-boards.greenhouse.io/example/jobs/123"

    class MutablePage:
        target_id = "page-42"

        def __init__(self) -> None:
            self.values = {"#first-name": ""}
            self.operations: list[tuple[str, str, str]] = []
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def read_only_snapshot(self) -> dict[str, object]:
            return {
                "target_id": self.target_id,
                "url": page_url,
                "html": "<html>sanitized fixture</html>",
                "read_only": True,
            }

        def replace_text(self, selector: str, value: str) -> None:
            self.operations.append(("replace_text", selector, value))
            self.values[selector] = value

        def read_value(self, selector: str) -> str:
            return self.values[selector]

    page = MutablePage()
    bindings: list[tuple[str, str]] = []

    class Transport:
        def __init__(self, base_url: str) -> None:
            self.base_url = base_url

        def bind_mutable_page_target(self, target_id: str):
            bindings.append((self.base_url, target_id))
            return page

    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({"personal": {"first_name": "Kevin"}}), encoding="utf-8")
    answers_path = tmp_path / "approved-answers.json"
    answers_path.write_text(json.dumps({"#first-name": "Kevin"}), encoding="utf-8")
    output_path = tmp_path / "review-evidence.json"
    dispatched: list[dict] = []

    def prepare(**kwargs):
        dispatched.append(kwargs)
        return {
            "platform": "greenhouse",
            "company": "Example Inc",
            "role": "Software Engineer Intern",
            "requisition": "123",
            "questions": [{"label": "First name", "required": True}],
            "submission_enabled": False,
        }

    exit_code = prepare_live_job.main(
        [
            "--cdp-base-url", "http://127.0.0.1:9222",
            "--target-id", "page-42",
            "--expected-url", page_url,
            "--company", "Example Inc",
            "--role", "Software Engineer Intern",
            "--requisition", "123",
            "--profile", str(profile_path),
            "--approved-answers", str(answers_path),
            "--output", str(output_path),
        ],
        transport_factory=Transport,
        prepare=prepare,
        coverage=lambda **kwargs: {
            "known": [{"question": "First name", "question_key": "first_name", "source": "profile"}],
            "company_specific": [],
            "optional_skip": [],
            "human_required": [],
        },
    )

    assert exit_code == 0
    assert bindings == [("http://127.0.0.1:9222", "page-42")]
    assert dispatched == [{"html_text": "<html>sanitized fixture</html>", "page_url": page_url}] * 2
    assert page.operations == [("replace_text", "#first-name", "Kevin")]
    assert page.closed is True
    persisted_text = output_path.read_text(encoding="utf-8")
    assert "Kevin" not in persisted_text
    persisted = json.loads(persisted_text)
    assert persisted["submission_enabled"] is False
    assert persisted["review_ready"] is True
    assert persisted["applied_answers"] == {
        "action": "fill_known_page",
        "field_evidence": [{
            "action": "replace_text",
            "selector": "#first-name",
            "verified": True,
            "target_id": "page-42",
            "target_url": page_url,
        }],
        "verified": True,
    }
    assert persisted["evidence"] == {
        "sanitized": True,
        "target_bound": True,
        "answer_values_persisted": False,
    }
    assert json.loads(capsys.readouterr().out) == persisted
