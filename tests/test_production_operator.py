from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_local_demo_cli_requires_explicit_sanitized_submit_approval_before_runner(
    tmp_path, capsys
):
    import production_operator

    called = False

    def forbidden_runner(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("demo runner must not start without explicit approval")

    result = production_operator.main(
        [
            "local-demo",
            "--resume",
            str(tmp_path / "Resume.pdf"),
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--output",
            str(tmp_path / "operator-report.json"),
        ],
        demo_runner=forbidden_runner,
    )

    assert result == 2
    assert called is False
    assert not (tmp_path / "operator-report.json").exists()
    assert json.loads(capsys.readouterr().out) == {
        "error": "explicit sanitized-demo submit approval is required",
        "real_application_authorized": False,
        "report_persisted": False,
    }


def test_cli_runs_and_audits_full_sanitized_learned_ats_operator_under_targets(
    tmp_path, capsys
):
    import production_operator

    resume = tmp_path / "Resume.pdf"
    resume.write_bytes(b"%PDF-1.7\nsanitized operator demonstration resume")
    output = tmp_path / "operator-report.json"

    result = production_operator.main(
        [
            "local-demo",
            "--resume",
            str(resume),
            "--runtime-dir",
            str(tmp_path / "operator-runtime"),
            "--output",
            str(output),
            "--approve-sanitized-submit",
        ]
    )

    assert result == 0
    stdout_report = json.loads(capsys.readouterr().out)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert stdout_report == persisted
    assert persisted["schema_version"] == 1
    assert persisted["mode"] == "sanitized_local_demo"
    assert persisted["platform"] == "greenhouse"
    assert persisted["status"] == "complete"
    assert persisted["health_gates"]["passed"] is True
    assert all(persisted["health_gates"]["checks"].values())
    assert persisted["timing"]["preparation_seconds"] < 300
    assert persisted["timing"]["verified_submission_seconds"] < 600
    assert persisted["timing"]["within_targets"] is True
    assert {item["stage"] for item in persisted["timing"]["stages"]} == {
        "discovery",
        "upload",
        "form_fill",
        "review",
        "confirmation",
        "tracker_readback",
        "discord_readback",
    }
    assert persisted["evidence"] == {
        "authorization_consumed": True,
        "discord_readback_verified": True,
        "fields_verified": 7,
        "learned_map_version": 1,
        "one_shot": True,
        "portal_confirmed": True,
        "review_authoritative": True,
        "single_use_replay_denied": True,
        "submit_count": 1,
        "tracker_readback_verified": True,
    }
    assert persisted["safety"] == {
        "credentials_accessed": False,
        "external_message_sent": False,
        "live_tracker_mutated": False,
        "production_ats_accessed": False,
        "real_application_authorized": False,
        "real_application_submitted": False,
        "sanitized_fixture_only": True,
    }
    assert persisted["final_audit"] == {
        "checks_passed": True,
        "real_application_authorized": False,
        "status": "ready_for_manual_live_authorization_review",
    }

    serialized = json.dumps(persisted, sort_keys=True)
    assert "Fixture Person" not in serialized
    assert str(tmp_path) not in serialized
    assert "token" not in serialized.casefold()

    assert production_operator.main(["audit", "--report", str(output)]) == 0
    assert json.loads(capsys.readouterr().out) == persisted["final_audit"]


@pytest.mark.parametrize(
    ("section", "key", "unsafe_value"),
    [
        ("health_gates", "passed", False),
        ("timing", "within_targets", False),
        ("safety", "production_ats_accessed", True),
        ("evidence", "submit_count", 2),
    ],
)
def test_final_operator_audit_fails_closed_on_health_timing_safety_or_replay_drift(
    section, key, unsafe_value
):
    import production_operator

    report = production_operator.empty_verified_report_for_test()
    report[section][key] = unsafe_value

    assert production_operator.audit_operator_report(report) == {
        "checks_passed": False,
        "real_application_authorized": False,
        "status": "not_ready",
    }
