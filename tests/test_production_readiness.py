from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import production_readiness


def test_audit_requires_all_supported_fixture_flows_and_reports_human_only_gates():
    report = production_readiness.build_audit(
        dry_run_verification={
            "idempotent_queueing": True,
            "unsupported_roles_not_queued": True,
            "submission_enabled": False,
            "external_side_effects_blocked": True,
        },
        fixture_flows={
            platform: {"submission_enabled": False, "plan": {"platform": platform}}
            for platform in ("greenhouse", "workday", "lever", "oracle")
        },
    )

    assert report["status"] == "ready_for_human_gated_production"
    assert report["dry_run_verified"] is True
    assert report["fixture_flows_verified"] == ["greenhouse", "lever", "oracle", "workday"]
    assert report["human_only_gates"] == [
        "CAPTCHA",
        "identity_verification",
        "passkey_or_biometric_prompt",
        "mfa_push_or_device_approval",
        "assessments",
        "unknown_required_questions",
        "explicit_submission_authorization",
    ]


def test_main_reads_persisted_non_submitting_evidence_and_emits_audit(tmp_path, capsys):
    dry_run_path = tmp_path / "dry-run.json"
    flows_path = tmp_path / "fixture-flows.json"
    dry_run_path.write_text(
        json.dumps(
            {
                "verification": {
                    "idempotent_queueing": True,
                    "unsupported_roles_not_queued": True,
                    "submission_enabled": False,
                    "external_side_effects_blocked": True,
                }
            }
        )
    , encoding="utf-8")
    flows_path.write_text(
        json.dumps(
            {
                platform: {"submission_enabled": False, "plan": {"platform": platform}}
                for platform in ("greenhouse", "workday", "lever", "oracle")
            }
        )
    , encoding="utf-8")

    exit_code = production_readiness.main(
        ["--dry-run-report", str(dry_run_path), "--fixture-flows", str(flows_path)]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready_for_human_gated_production"


def test_audit_accepts_verified_learned_ats_speed_evidence_with_safety_invariants():
    report = production_readiness.build_audit(
        dry_run_verification={
            "idempotent_queueing": True,
            "unsupported_roles_not_queued": True,
            "submission_enabled": False,
            "external_side_effects_blocked": True,
        },
        fixture_flows={
            platform: {"submission_enabled": False, "plan": {"platform": platform}}
            for platform in ("greenhouse", "workday", "lever", "oracle")
        },
        learned_ats_benchmark={
            "platform": "njoyn",
            "preparation_seconds": 299,
            "verified_submission_seconds": 599,
            "submission_enabled": False,
            "external_side_effects_blocked": True,
            "parser_repair_required": True,
            "review_required": True,
            "confirmation_required": True,
            "tracker_readback_required": True,
            "discord_readback_required": True,
        },
    )

    assert report["learned_ats_benchmark"] == {
        "platform": "njoyn",
        "preparation_within_target": True,
        "verified_submission_within_target": True,
        "safety_invariants_verified": True,
        "ready": True,
    }




def test_audit_requires_passing_canaries_for_every_learned_executor():
    report = production_readiness.build_audit(
        dry_run_verification={
            "idempotent_queueing": True,
            "unsupported_roles_not_queued": True,
            "submission_enabled": False,
            "external_side_effects_blocked": True,
        },
        fixture_flows={
            platform: {"submission_enabled": False, "plan": {"platform": platform}}
            for platform in ("greenhouse", "workday", "lever", "oracle")
        },
        browser_canaries={
            platform: {"status": "passed", "submission_enabled": False}
            for platform in ("njoyn", "greenhouse", "workday", "lever", "oracle")
        },
    )

    assert report["browser_canaries_verified"] == [
        "greenhouse", "lever", "njoyn", "oracle", "workday"
    ]
    assert report["status"] == "ready_for_human_gated_production"


def test_main_reads_persisted_learned_ats_benchmark_evidence(tmp_path, capsys):
    dry_run_path = tmp_path / "dry-run.json"
    flows_path = tmp_path / "fixture-flows.json"
    benchmark_path = tmp_path / "njoyn-benchmark.json"
    dry_run_path.write_text(json.dumps({"verification": {
        "idempotent_queueing": True,
        "unsupported_roles_not_queued": True,
        "submission_enabled": False,
        "external_side_effects_blocked": True,
    }}), encoding="utf-8")
    flows_path.write_text(json.dumps({
        platform: {"submission_enabled": False, "plan": {"platform": platform}}
        for platform in ("greenhouse", "workday", "lever", "oracle")
    }), encoding="utf-8")
    benchmark_path.write_text(json.dumps({
        "platform": "njoyn",
        "preparation_seconds": 299,
        "verified_submission_seconds": 599,
        "submission_enabled": False,
        "external_side_effects_blocked": True,
        "parser_repair_required": True,
        "review_required": True,
        "confirmation_required": True,
        "tracker_readback_required": True,
        "discord_readback_required": True,
    }), encoding="utf-8")

    exit_code = production_readiness.main([
        "--dry-run-report", str(dry_run_path),
        "--fixture-flows", str(flows_path),
        "--learned-ats-benchmark", str(benchmark_path),
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["learned_ats_benchmark"]["ready"] is True
