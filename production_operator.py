#!/usr/bin/env python3
"""Approval-gated production-operator proof over one sanitized local ATS fixture.

This command intentionally exposes no real-application mode.  It composes the
production contracts end to end against one exact marker-checked static page so
health, timing, authorization, submission recovery, and downstream read-back
evidence can be audited before a human separately authorizes any live use.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import browser_integration_canary
import browser_health
import confirmation_reconciliation
import greenhouse_handler
import live_confirmation_reader
import live_delivery_adapters
import live_review_reader
import live_run_manifest
import notifier
import one_shot_submit
import platform_io
import post_submit_transaction
import prepare_live_job
import resume_preflight
import review_reconciler
from scoped_cdp import ScopedCDPTransport
import submission_authorization
import tenant_field_maps
import timing_telemetry
from local_cdp_operator import LocalChromeFixtureSession, find_local_chrome_for_testing
from production_readiness import PREPARATION_TARGET_SECONDS, VERIFIED_SUBMISSION_TARGET_SECONDS


ROOT = Path(__file__).resolve().parent
LOCAL_FIXTURE = (ROOT / "fixtures" / "local_operator_e2e.html").resolve()
LEARNED_PAGE_URL = "https://job-boards.greenhouse.io/fixture/REQ-123"
IDENTITY = {
    "company": "Sanitized Example",
    "role": "Software Engineer Intern",
    "requisition": "REQ-123",
}
REQUIRED_TIMING_STAGES = {
    "discovery",
    "upload",
    "form_fill",
    "review",
    "confirmation",
    "tracker_readback",
    "discord_readback",
}
SAFETY_EVIDENCE = {
    "credentials_accessed": False,
    "external_message_sent": False,
    "live_tracker_mutated": False,
    "production_ats_accessed": False,
    "real_application_authorized": False,
    "real_application_submitted": False,
    "sanitized_fixture_only": True,
}


class OperatorBlockedError(ValueError):
    """The local proof failed a mandatory pre-action or audit gate."""


class _TimedLocalTracker:
    """In-memory tracker proof with the Task 7 adapter shape."""

    def __init__(self) -> None:
        self.record: dict | None = None
        self.elapsed_seconds = 0.0

    def _measure(self, operation: Callable[[], object]) -> object:
        started = time.monotonic()
        try:
            return operation()
        finally:
            self.elapsed_seconds += time.monotonic() - started

    def append(self, **kwargs) -> None:
        self._measure(lambda: setattr(self, "record", dict(kwargs)))

    def read_back(self, *, transaction_id: str) -> dict | None:
        def read() -> dict | None:
            if self.record is None:
                return None
            return {
                "verified": True,
                "transaction_id": transaction_id,
                "payload_sha256": self.record["payload_sha256"],
                "receipt_id": "sanitized-local-row",
            }

        return self._measure(read)  # type: ignore[return-value]


class _TimedLocalDiscord:
    """In-memory Discord proof with no network or external message API."""

    def __init__(self) -> None:
        self.record: dict | None = None
        self.elapsed_seconds = 0.0

    def _measure(self, operation: Callable[[], object]) -> object:
        started = time.monotonic()
        try:
            return operation()
        finally:
            self.elapsed_seconds += time.monotonic() - started

    def send(self, **kwargs) -> None:
        self._measure(lambda: setattr(self, "record", dict(kwargs)))

    def read_back(self, *, transaction_id: str) -> dict | None:
        def read() -> dict | None:
            if self.record is None:
                return None
            return {
                "verified": True,
                "transaction_id": transaction_id,
                "message_sha256": self.record["message_sha256"],
                "receipt_id": "sanitized-local-message",
            }

        return self._measure(read)  # type: ignore[return-value]


def _resume_preflight(path: Path) -> dict[str, object]:
    resolved = Path(path).resolve()
    if resolved.name != "Resume.pdf" or not resolved.is_file():
        raise OperatorBlockedError("sanitized demo requires an existing exact Resume.pdf")
    content = resolved.read_bytes()
    if not content.startswith(b"%PDF"):
        raise OperatorBlockedError("sanitized demo Resume.pdf failed PDF preflight")
    return {
        "path": resolved,
        "basename": "Resume.pdf",
        "content_type": "application/pdf",
        "sha256": hashlib.sha256(content).hexdigest(),
        "verified": True,
    }


def _prepare_greenhouse_fixture(*, html_text: str, page_url: str) -> dict:
    inspected = greenhouse_handler.inspect_html(html_text, page_url=page_url)
    if (
        inspected.get("page_type") != "application"
        or inspected.get("manual_gate") is not None
        or inspected.get("company") != IDENTITY["company"]
        or inspected.get("role") != IDENTITY["role"]
        or IDENTITY["requisition"] not in html_text
    ):
        raise OperatorBlockedError("sanitized learned-ATS handler health gate failed")
    return {
        **inspected,
        "platform": "greenhouse",
        **IDENTITY,
        "questions": [],
        "submission_enabled": False,
    }


def _known_coverage(**kwargs) -> dict[str, list[dict[str, str]]]:
    del kwargs
    return {
        "known": [
            {"question_key": key, "source": "explicit_sanitized_demo_map"}
            for key in (
                "first_name",
                "last_name",
                "email",
                "phone",
                "resume",
                "work_authorization",
                "sponsorship",
            )
        ],
        "company_specific": [],
        "optional_skip": [],
        "human_required": [],
    }


def _require_health_gates(checks: dict[str, bool]) -> dict[str, object]:
    if not checks or not all(value is True for value in checks.values()):
        raise OperatorBlockedError("operational health gate failed before sanitized submit")
    return {"passed": True, "checks": checks}


def _number_below(value: object, target: float) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= float(value) < target
    )


def audit_operator_report(report: object) -> dict[str, object]:
    """Re-evaluate a persisted local run without granting real submit authority."""
    valid = isinstance(report, dict)
    payload = report if isinstance(report, dict) else {}
    health = payload.get("health_gates", {})
    timing = payload.get("timing", {})
    evidence = payload.get("evidence", {})
    safety = payload.get("safety", {})
    valid = valid and all(
        (
            payload.get("schema_version") == 1,
            payload.get("mode") == "sanitized_local_demo",
            payload.get("platform") == "greenhouse",
            payload.get("status") == "complete",
            isinstance(health, dict),
            health.get("passed") is True,
            isinstance(health.get("checks"), dict),
            bool(health.get("checks")),
            all(value is True for value in health.get("checks", {}).values()),
            isinstance(timing, dict),
            timing.get("within_targets") is True,
            _number_below(timing.get("preparation_seconds"), PREPARATION_TARGET_SECONDS),
            _number_below(
                timing.get("verified_submission_seconds"),
                VERIFIED_SUBMISSION_TARGET_SECONDS,
            ),
            isinstance(timing.get("stages"), list),
            {
                item.get("stage")
                for item in timing.get("stages", [])
                if isinstance(item, dict)
                and _number_below(item.get("elapsed_seconds"), float("inf"))
            }
            == REQUIRED_TIMING_STAGES,
            isinstance(evidence, dict),
            evidence.get("learned_map_version") == 1,
            evidence.get("fields_verified") == 7,
            evidence.get("review_authoritative") is True,
            evidence.get("authorization_consumed") is True,
            evidence.get("single_use_replay_denied") is True,
            evidence.get("one_shot") is True,
            evidence.get("submit_count") == 1,
            evidence.get("portal_confirmed") is True,
            evidence.get("tracker_readback_verified") is True,
            evidence.get("discord_readback_verified") is True,
            safety == SAFETY_EVIDENCE,
        )
    )
    return {
        "checks_passed": bool(valid),
        "real_application_authorized": False,
        "status": "ready_for_manual_live_authorization_review" if valid else "not_ready",
    }


def audit_live_release_evidence(evidence: object) -> dict[str, object]:
    """Validate the closed, value-free local release record without enabling live use."""
    valid = isinstance(evidence, dict)
    payload = evidence if isinstance(evidence, dict) else {}
    queue_value = payload.get("queue")
    queue = queue_value if isinstance(queue_value, dict) else {}
    verification_value = payload.get("verification")
    verification = verification_value if isinstance(verification_value, dict) else {}
    full_suite_value = verification.get("full_suite")
    full_suite = full_suite_value if isinstance(full_suite_value, dict) else {}
    local_chrome_value = verification.get("local_chrome")
    local_chrome = local_chrome_value if isinstance(local_chrome_value, dict) else {}
    preflight_value = verification.get("normal_chrome_preflight")
    preflight = preflight_value if isinstance(preflight_value, dict) else {}
    documentation_value = payload.get("documentation")
    documentation = documentation_value if isinstance(documentation_value, dict) else {}
    passed = full_suite.get("passed")
    valid = valid and all(
        (
            set(payload) == {
                "schema_version",
                "mode",
                "queue",
                "verification",
                "documentation",
                "safety",
            },
            payload.get("schema_version") == 1,
            payload.get("mode") == "sanitized_local_release",
            isinstance(queue_value, dict),
            set(queue) == {"heading", "tasks_completed", "ordered_task_commits"},
            queue.get("heading") == "Unified live-production CLI queue — completed",
            queue.get("tasks_completed") == 10,
            queue.get("ordered_task_commits") is True,
            isinstance(verification_value, dict),
            set(verification)
            == {
                "focused_tdd_passed",
                "full_suite",
                "local_chrome",
                "normal_chrome_preflight",
                "git_diff_check",
                "forbidden_executable_source_matches",
                "clean_worktree",
            },
            verification.get("focused_tdd_passed") is True,
            isinstance(full_suite_value, dict),
            set(full_suite) == {"command", "passed"},
            full_suite.get("command") == "python -m pytest tests -q",
            isinstance(passed, int) and not isinstance(passed, bool) and passed >= 306,
            isinstance(local_chrome_value, dict),
            local_chrome
            == {
                "passed": True,
                "sanitized_fixture_only": True,
                "submit_count": 1,
                "replay_denied": True,
            },
            isinstance(preflight_value, dict),
            preflight
            == {
                "passed": True,
                "read_only": True,
                "exact_target_attached": True,
                "content_unchanged": True,
                "submission_enabled": False,
            },
            verification.get("git_diff_check") is True,
            verification.get("forbidden_executable_source_matches") == [],
            verification.get("clean_worktree") is True,
            isinstance(documentation_value, dict),
            documentation
            == {
                "readme_updated": True,
                "operations_updated": True,
                "job_application_skill_updated": True,
            },
            payload.get("safety") == SAFETY_EVIDENCE,
        )
    )
    return {
        "checks_passed": bool(valid),
        "commit_external_enabled": False,
        "real_application_authorized": False,
        "real_live_enabled": False,
        "status": "ready_for_manual_live_authorization_review" if valid else "not_ready",
    }


def run_live_release_audit(*, evidence_path: Path) -> dict[str, object]:
    """Read and audit a local release artifact; this command grants no capability."""
    try:
        evidence = json.loads(Path(evidence_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        evidence = None
    return audit_live_release_evidence(evidence)


def empty_verified_report_for_test() -> dict:
    """Return a value-free verified shape for focused fail-closed audit tests."""
    report = {
        "schema_version": 1,
        "mode": "sanitized_local_demo",
        "platform": "greenhouse",
        "status": "complete",
        "health_gates": {"passed": True, "checks": {"fixture": True}},
        "timing": {
            "preparation_seconds": 1.0,
            "verified_submission_seconds": 2.0,
            "within_targets": True,
            "stages": [timing_telemetry.record_stage(stage, 0) for stage in sorted(REQUIRED_TIMING_STAGES)],
        },
        "evidence": {
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
        },
        "safety": deepcopy(SAFETY_EVIDENCE),
    }
    return report


def run_local_sanitized_demo(
    *, resume_path: Path, runtime_dir: Path, approved: bool
) -> dict:
    """Compose all production stages against one exact sanitized local page."""
    if approved is not True:
        raise OperatorBlockedError("explicit sanitized-demo submit approval is required")
    if LOCAL_FIXTURE.resolve() != (ROOT / "fixtures" / "local_operator_e2e.html").resolve():
        raise OperatorBlockedError("sanitized fixture binding changed")
    fixture_text = LOCAL_FIXTURE.read_text(encoding="utf-8")
    if 'data-fixture="local-operator"' not in fixture_text:
        raise OperatorBlockedError("sanitized fixture marker is missing")

    resume = _resume_preflight(Path(resume_path))
    runtime = Path(runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    mapping = tenant_field_maps.resolve_field_map(
        page_url=LEARNED_PAGE_URL,
        platform="greenhouse",
    )
    approved_answers = {
        "first_name": "Fixture Person",
        "last_name": "Example",
        "email": "fixture.person@example.test",
        "phone": "+1-555-0100",
        "resume": str(resume["path"]),
        "work_authorization": "Yes",
        "sponsorship": "No",
    }
    actions = tenant_field_maps.build_step_actions(
        mapping=mapping,
        step="application",
        approved_answers=approved_answers,
    )
    transition = tenant_field_maps.plan_next_step(
        mapping=mapping,
        current_step="application",
        completed_fields=set(approved_answers),
        conditions={},
    )
    if transition.get("status") != "advance" or transition.get("next_step") != "review":
        raise OperatorBlockedError("learned ATS application step was not Review-ready")

    stage_seconds: dict[str, float] = {}
    preparation_started = time.monotonic()
    discovery_started = time.monotonic()
    session_context = LocalChromeFixtureSession(
        fixture_path=LOCAL_FIXTURE,
        chrome_path=find_local_chrome_for_testing(),
        runtime_dir=runtime / "chrome",
    )
    with session_context as session:
        stage_seconds["discovery"] = time.monotonic() - discovery_started
        target_id = session.target_id
        with session.bind_exact_page(target_id) as page:
            page.set_retina_scale_for_test(2.0)
            initial_snapshot = page.read_only_snapshot()
            canary = browser_integration_canary.run_canary(
                "greenhouse",
                {
                    "retina_scale": initial_snapshot.get("device_scale_factor"),
                    "target_current": initial_snapshot.get("target_id") == target_id,
                    "control_visible": True,
                    "overlay_present": initial_snapshot.get("overlay_present"),
                    "native_window_detected": False,
                    "submission_enabled": False,
                },
            )

            def apply_learned_answers(_: dict[str, object]) -> dict[str, object]:
                form_actions = [item for item in actions if item["operation"] != "cdp_upload"]
                upload_actions = [item for item in actions if item["operation"] == "cdp_upload"]
                form_started = time.monotonic()
                form_result = tenant_field_maps.execute_step_actions(
                    page=page,
                    target_id=target_id,
                    expected_url=session.page_url,
                    actions=form_actions,
                )
                stage_seconds["form_fill"] = time.monotonic() - form_started
                upload_started = time.monotonic()
                upload_result = tenant_field_maps.execute_step_actions(
                    page=page,
                    target_id=target_id,
                    expected_url=session.page_url,
                    actions=upload_actions,
                )
                stage_seconds["upload"] = time.monotonic() - upload_started
                evidence = [
                    *form_result.get("field_evidence", []),
                    *upload_result.get("field_evidence", []),
                ]
                return {
                    "action": "apply_learned_step",
                    "field_evidence": evidence,
                    "verified": len(evidence) == 7
                    and all(item.get("verified") is True for item in evidence),
                }

            prepared = prepare_live_job.prepare_live_job(
                page=page,
                target_id=target_id,
                expected_url=session.page_url,
                expected_identity=IDENTITY,
                profile={},
                prepare=_prepare_greenhouse_fixture,
                coverage=_known_coverage,
                approved_answers=approved_answers,
                apply_known=apply_learned_answers,
            )
            prepared = prepare_live_job._sanitize_review_evidence(prepared)
            field_evidence = prepared["applied_answers"]["field_evidence"]
            page.wait_for_resume_sha256(str(resume["sha256"]))

            review_started = time.monotonic()
            page.activate_review("#review")
            profile_fields = {
                "#first_name": approved_answers["first_name"],
                "#last_name": approved_answers["last_name"],
                "#email": approved_answers["email"],
                "#phone": approved_answers["phone"],
                "#authorization": approved_answers["work_authorization"],
                "#sponsorship": approved_answers["sponsorship"],
            }
            authoritative = review_reconciler.reconcile_review(
                preparation_evidence=prepared,
                server_review=page.read_server_review(),
                expected_target={
                    "target_id": target_id,
                    "page_url": session.page_url,
                    **IDENTITY,
                },
                profile_fields=profile_fields,
                resume_preflight={
                    key: resume[key]
                    for key in ("basename", "content_type", "sha256", "verified")
                },
                required_parser_repairs=[],
                required_question_ids=["work_authorization"],
            )
            stage_seconds["review"] = time.monotonic() - review_started
            preparation_seconds = time.monotonic() - preparation_started

            health_gates = _require_health_gates(
                {
                    "explicit_sanitized_approval": approved is True,
                    "exact_static_fixture": LOCAL_FIXTURE.is_file(),
                    "chrome_process": "Chrome" in session.browser_product,
                    "process_scoped_cdp_pipe": True,
                    "exact_target_current": initial_snapshot.get("url") == session.page_url,
                    "retina_canary": canary.get("status") == "passed",
                    "mandatory_gates_clear": initial_snapshot.get("gates") == [],
                    "maango_clear": initial_snapshot.get("maango") is False,
                    "exact_resume_preflight": resume.get("verified") is True,
                    "learned_map_exact": mapping.get("version") == 1
                    and mapping.get("tenant") == "fixture",
                    "all_fields_read_back": len(field_evidence) == 7
                    and all(item.get("verified") is True for item in field_evidence),
                    "authoritative_review": authoritative.get("review_authoritative") is True,
                    "local_downstream_adapters": True,
                }
            )

            issued_at = datetime.now(timezone.utc).replace(microsecond=0)
            authorization_store = submission_authorization.SubmissionAuthorizationStore(
                runtime / "submission-authorization.db"
            )
            issued = authorization_store.issue(
                job_id=1,
                review_evidence=authoritative,
                actor="sanitized-local-operator",
                issued_at=issued_at,
                expires_at=issued_at + timedelta(minutes=5),
            )

            submission_started = time.monotonic()
            confirmation_started = time.monotonic()
            submitted = one_shot_submit.execute_one_shot_submit(
                authorization_store=authorization_store,
                token=issued["token"],
                page=page,
                journal_path=runtime / "page-actions.jsonl",
                job_id=1,
                target_id=target_id,
                expected_url=session.page_url,
                requisition=IDENTITY["requisition"],
                review_evidence_sha256=authoritative["review_evidence_sha256"],
                actor="sanitized-local-operator",
                now=issued_at + timedelta(seconds=1),
                submit_selector="#submit",
            )
            if submitted.get("status") != "confirmation_observed":
                raise OperatorBlockedError("one-shot sanitized submit lacked confirmation")
            try:
                authorization_store.consume(
                    token=issued["token"],
                    current_binding={
                        "job_id": 1,
                        "target_id": target_id,
                        "page_url": session.page_url,
                        "requisition": IDENTITY["requisition"],
                        "review_evidence_sha256": authoritative["review_evidence_sha256"],
                    },
                    actor="sanitized-local-operator",
                    now=issued_at + timedelta(seconds=2),
                )
            except PermissionError as exc:
                replay_denied = "replayed" in str(exc)
            else:
                replay_denied = False

            confirmation = confirmation_reconciliation.extract_confirmation(
                platform="greenhouse",
                html_text=page.read_only_snapshot()["html"],
                page_url=session.page_url,
            )
            portal = confirmation_reconciliation.reconcile_candidate_portal(
                confirmation=confirmation,
                expected_identity=IDENTITY,
                candidate_applications=page.read_candidate_applications(),
            )
            submit_count = page.read_submit_count()
            stage_seconds["confirmation"] = time.monotonic() - confirmation_started

    tracker = _TimedLocalTracker()
    discord = _TimedLocalDiscord()
    final_transaction = post_submit_transaction.PostSubmitTransactionCoordinator(
        state_path=runtime / "post-submit.db",
        tracker=tracker,
        discord=discord,
    ).run(
        job_id=1,
        portal_evidence=portal,
        tracker_payload={"status": "Submitted - Pending Response"},
        discord_message="Sanitized local operator demonstration complete",
    )
    stage_seconds["tracker_readback"] = tracker.elapsed_seconds
    stage_seconds["discord_readback"] = discord.elapsed_seconds
    verified_submission_seconds = time.monotonic() - submission_started

    report = {
        "schema_version": 1,
        "mode": "sanitized_local_demo",
        "platform": "greenhouse",
        "status": "complete",
        "health_gates": health_gates,
        "timing": {
            "preparation_seconds": preparation_seconds,
            "verified_submission_seconds": verified_submission_seconds,
            "within_targets": preparation_seconds < PREPARATION_TARGET_SECONDS
            and verified_submission_seconds < VERIFIED_SUBMISSION_TARGET_SECONDS,
            "stages": [
                timing_telemetry.record_stage(stage, stage_seconds[stage])
                for stage in sorted(REQUIRED_TIMING_STAGES)
            ],
        },
        "evidence": {
            "authorization_consumed": submitted.get("authorization_consumed") is True,
            "discord_readback_verified": final_transaction.get("discord", {}).get(
                "readback_verified"
            )
            is True,
            "fields_verified": len(field_evidence),
            "learned_map_version": mapping.get("version"),
            "one_shot": submitted.get("one_shot") is True,
            "portal_confirmed": portal.get("portal_confirmed") is True,
            "review_authoritative": authoritative.get("review_authoritative") is True,
            "single_use_replay_denied": replay_denied,
            "submit_count": submit_count,
            "tracker_readback_verified": final_transaction.get("tracker", {}).get(
                "readback_verified"
            )
            is True,
        },
        "safety": deepcopy(SAFETY_EVIDENCE),
    }
    report["final_audit"] = audit_operator_report(report)
    if report["final_audit"]["checks_passed"] is not True:
        raise OperatorBlockedError("final sanitized operator audit failed")
    return report


DemoRunner = Callable[..., dict]
LiveTransportFactory = Callable[[str], object]
LiveReadOnlyTransportFactory = Callable[[str], object]
LiveHealthProbe = Callable[[str], dict]
LiveCoverage = Callable[..., dict]
LiveClock = Callable[[], datetime]


def _load_runtime_object(path: Path | str, *, label: str) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorBlockedError(f"{label} could not be read") from exc
    if not isinstance(payload, dict):
        raise OperatorBlockedError(f"{label} must be a JSON object")
    return payload


def _verify_manifest_file(path: Path | str, expected_sha256: str, *, label: str) -> None:
    try:
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise OperatorBlockedError(f"{label} could not be read") from exc
    if actual != expected_sha256:
        raise OperatorBlockedError(f"{label} evidence drift detected")


def _authorization_store(manifest: dict):
    """Production cannot choose a fresh per-job database to evade fencing."""
    production = manifest["mode"] == "production_live"
    if production:
        return submission_authorization.SubmissionAuthorizationStore(
            Path.home() / "Documents/job-agent/runtime/submission-ledger.sqlite3",
            production=True,
        )
    return submission_authorization.SubmissionAuthorizationStore(
        manifest["runtime_paths"]["authorization_db"]
    )


def _verify_employer_approval(manifest: dict) -> None:
    """Recompute restricted-employer membership, not a caller's boolean."""
    from scanner import maango_company
    restricted = bool(maango_company(manifest["identity"]["company"], manifest["target"]["url"]))
    gate = manifest["manual_gate"]
    if restricted and (gate.get("maango") is not True or gate.get("maango_approved") is not True):
        raise OperatorBlockedError("exact MAANGO employer approval is required before mutation")


def _authorization_identity(manifest: dict, profile: dict) -> dict:
    if manifest["mode"] != "production_live":
        return {}
    email = profile.get("contact", {}).get("email")
    if not isinstance(email, str) or not email.strip():
        raise OperatorBlockedError("canonical application account identity is missing")
    return {"account_id": email.strip().casefold(),
            "tenant": manifest["identity"]["platform"] + ":" + manifest["identity"]["tenant"]}


def _verified_profile_resume(manifest: dict) -> tuple[dict, dict]:
    profile_binding = manifest["profile"]
    resume_binding = manifest["resume"]
    profile_path = Path(profile_binding["path"])
    if (manifest["mode"] == "production_live"
            and profile_path.resolve() != (Path.home() / "Documents/job-agent/profile.json").resolve()):
        raise OperatorBlockedError("production requires the approved canonical profile path")
    _verify_manifest_file(profile_path, profile_binding["sha256"], label="profile")
    profile = _load_runtime_object(profile_path, label="profile")
    try:
        evidence = resume_preflight.preflight_profile_resume(profile_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise OperatorBlockedError("profile-selected Resume.pdf preflight failed") from exc
    expected = {
        "path": str(Path(resume_binding["path"]).resolve()),
        "basename": resume_binding["basename"],
        "content_type": resume_binding["content_type"],
        "sha256": resume_binding["sha256"],
        "verified": resume_binding["verified"],
    }
    observed = {
        "path": str(Path(evidence["path"]).resolve()),
        "basename": evidence["basename"],
        "content_type": evidence["content_type"],
        "sha256": evidence["sha256"],
        "verified": evidence["verified"],
    }
    if observed != expected:
        raise OperatorBlockedError("profile-selected Resume.pdf evidence drift detected")
    return profile, evidence


def _canary_selectors_for_actions(
    *, page: object, actions: list[dict[str, object]], expected_resume_basename: str
) -> list[str]:
    """Require visible controls except an already verified exact resume slot.

    Greenhouse replaces its file input after a successful upload.  The replacement
    view can still render the exact application resume filename, but has no file
    control to inspect or overwrite.  Preserve that verified slot; every other
    action remains part of the strict visible-control canary.
    """
    selectors: list[str] = []
    reader = getattr(page, "read_uploaded_filename", None)
    for action in actions:
        selector = action.get("selector")
        if not isinstance(selector, str) or not selector:
            raise OperatorBlockedError("learned action selector is invalid")
        if action.get("operation") == "cdp_upload" and callable(reader):
            try:
                existing = reader(selector)
            except (KeyError, OSError, ValueError, RuntimeError):
                existing = ""
            if existing == expected_resume_basename:
                continue
        selectors.append(selector)
    if not selectors:
        raise OperatorBlockedError("live preparation requires at least one visible learned control")
    return selectors


def run_live_prepare(
    *,
    manifest_path: Path,
    approved_answers_path: Path,
    step: str,
    cdp_base_url: str,
    production_enabled: bool,
    transport_factory: LiveTransportFactory = ScopedCDPTransport,
    health_probe: LiveHealthProbe = browser_health.probe_cdp_health,
    coverage: LiveCoverage = prepare_live_job.build_coverage_matrix,
) -> dict:
    """Prepare one manifest-bound page without navigation or submission."""
    prepare_live_job._validate_local_cdp_base_url(cdp_base_url)
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    _verify_employer_approval(manifest)
    profile, resume = _verified_profile_resume(manifest)
    approved_answers = _load_runtime_object(
        approved_answers_path, label="approved answers"
    )
    resume_answer = approved_answers.get("resume")
    if resume_answer is not None and Path(str(resume_answer)).resolve() != Path(
        resume["path"]
    ).resolve():
        raise OperatorBlockedError("approved resume differs from profile-selected Resume.pdf")
    approved_answers["resume"] = str(Path(resume["path"]).resolve())
    from schonfeld_form import URL as CLIENT_FORM_URL, verify_profile_answers
    if manifest["target"]["url"] == CLIENT_FORM_URL:
        verify_profile_answers(profile, approved_answers)
    else:
        from canonical_answers import verify_profile_answers as verify_canonical_answers
        verify_canonical_answers(profile, approved_answers, tenant=manifest["identity"]["tenant"])

    identity = manifest["identity"]
    target = manifest["target"]
    mapping = tenant_field_maps.resolve_field_map(
        page_url=target["url"], platform=identity["platform"]
    )
    if mapping.get("tenant") != identity["tenant"]:
        raise OperatorBlockedError("learned tenant differs from manifest identity")
    actions = tenant_field_maps.build_step_actions(
        mapping=mapping,
        step=step,
        approved_answers=approved_answers,
    )
    if not any(action.get("field") == "resume" for action in actions):
        raise OperatorBlockedError("live prepare step must upload profile-selected Resume.pdf")
    transition = tenant_field_maps.plan_next_step(
        mapping=mapping,
        current_step=step,
        completed_fields=set(approved_answers),
        conditions={},
    )
    if transition.get("status") == "human_required":
        raise OperatorBlockedError("learned step conditions remain human-required")

    health = health_probe(cdp_base_url)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise OperatorBlockedError("loopback Chrome CDP health gate failed")

    transport = transport_factory(cdp_base_url)
    with transport.bind_mutable_page_target(target["id"]) as page:  # type: ignore[attr-defined]
        snapshot = page.read_only_snapshot()
        if (
            snapshot.get("read_only") is not True
            or snapshot.get("target_id") != target["id"]
            or snapshot.get("url") != target["url"]
        ):
            raise OperatorBlockedError("exact target binding drift detected")
        selectors = _canary_selectors_for_actions(
            page=page,
            actions=actions,
            expected_resume_basename=str(resume["basename"]),
        )
        surface_reader = getattr(page, "inspect_safety_surface", None)
        if not callable(surface_reader):
            raise OperatorBlockedError("exact-page browser canary evidence is unavailable")
        surface = surface_reader(selectors)
        canary = browser_integration_canary.run_canary(
            identity["platform"],
            {
                **surface,
                "target_current": True,
                "submission_enabled": False,
            },
        )
        if canary.get("status") != "passed":
            raise OperatorBlockedError(
                f"exact-page browser canary blocked: {canary.get('reason', 'unknown')}"
            )

        expected_identity = {
            key: identity[key] for key in ("company", "role", "requisition")
        }

        def dispatch(**kwargs):
            return prepare_live_job._dispatch_live_html(
                **kwargs,
                expected_identity=expected_identity,
                expected_platform=identity["platform"],
                official_posting=snapshot.get("official_posting"),
            )

        prepared = prepare_live_job.prepare_live_job(
            page=page,
            target_id=target["id"],
            expected_url=target["url"],
            expected_identity=expected_identity,
            profile=profile,
            prepare=dispatch,
            coverage=coverage,
            approved_answers=approved_answers,
            apply_known=lambda _: tenant_field_maps.execute_step_actions(
                page=page,
                target_id=target["id"],
                expected_url=target["url"],
                actions=actions,
            ),
        )

    live_run_manifest.validate_manifest(
        manifest,
        production_enabled=production_enabled,
        observed_binding={
            "target_id": prepared["target_id"],
            "page_url": prepared["page_url"],
            **prepared["identity"],
            "platform": prepared["platform"],
            "tenant": mapping["tenant"],
        },
    )
    sanitized = {
        **prepare_live_job._sanitize_review_evidence(prepared),
        "status": "prepared",
        "manifest_binding": {
            "schema_version": manifest["schema_version"],
            "mode": manifest["mode"],
            "job_id": manifest["job_id"],
            "queue_id": manifest["queue_id"],
            "tenant": identity["tenant"],
        },
        "health": {
            "browser_ready": True,
            "canary_passed": True,
            "exact_target_bound": True,
        },
        "learned_map": {"version": mapping["version"], "tenant": mapping["tenant"]},
        "resume": {"basename": resume["basename"], "verified": True},
    }
    from schonfeld_form import URL as SCHONFELD_URL
    if target["url"] == SCHONFELD_URL:
        sanitized["evidence"]["input_binding"] = _client_input_binding(manifest, approved_answers)
        sanitized["evidence"]["prepared_at"] = datetime.now(timezone.utc).isoformat()
    output = Path(manifest["runtime_paths"]["preparation"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(sanitized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return sanitized


def _client_input_binding(manifest: dict, answers: dict) -> dict:
    return {
        "profile_sha256": manifest["profile"]["sha256"],
        "resume_sha256": manifest["resume"]["sha256"],
        "approved_answers_sha256": hashlib.sha256(
            json.dumps(answers, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def _check_client_inputs(manifest: dict, preparation: dict, answers: dict) -> None:
    from schonfeld_form import URL
    if manifest["target"]["url"] != URL:
        return
    evidence = preparation.get("evidence", {})
    if evidence.get("input_binding") != _client_input_binding(manifest, answers):
        raise OperatorBlockedError("client profile/resume/approved input binding drift")
    try:
        observed = datetime.fromisoformat(evidence.get("prepared_at", ""))
        fresh = observed.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 600
    except (TypeError, ValueError):
        fresh = False
    if not fresh:
        raise OperatorBlockedError("fresh one-page preparation required (maximum 600 seconds)")


def _manifest_job_identity(manifest: dict) -> dict[str, object]:
    return {
        "job_id": manifest["job_id"],
        "queue_id": manifest["queue_id"],
        "target_id": manifest["target"]["id"],
        "page_url": manifest["target"]["url"],
        **manifest["identity"],
    }


def _review_summary(wrapper: dict) -> dict[str, object]:
    review = wrapper["review"]
    return {
        "status": wrapper["status"],
        "review_authoritative": review.get("review_authoritative") is True,
        "review_evidence_sha256": review.get("review_evidence_sha256"),
        "verified_fields": [
            item["field"]
            for item in review.get("fields", [])
            if isinstance(item, dict) and item.get("verified") is True
        ],
        "blockers": review.get("human_required", []),
        "job_identity": wrapper["job_identity"],
    }


def _independent_review_fields(profile: dict, mapping: dict, step: str, observed: dict) -> dict:
    """All populated and required controls are grounded without action values."""
    from canonical_answers import canonical_review_fields
    spec = mapping["steps"][step]
    controls = spec["controls"]
    # Canonical phone facts are digits; normalize only allowed presentation punctuation.
    phone = controls.get("phone", {}).get("selector")
    phone_value = observed.get("fields", {}).get(phone)
    if isinstance(phone_value, str) and all((c.isascii() and c.isdigit()) or c in '+(). -' for c in phone_value):
        observed["fields"][phone] = ''.join(c for c in phone_value if c.isdigit())
    selectors = set()
    for field in spec.get("required_fields", []):
        control = controls[field]
        if control.get("operation") not in ("cdp_upload", "submit"):
            selectors.add(control["selector"])
    for selector, evidence in observed.get("fields", {}).items():
        value = evidence.get("value") if isinstance(evidence, dict) else evidence
        if value is not None and str(value).strip():
            selectors.add(selector)
    return canonical_review_fields(profile, controls, selectors=sorted(selectors), tenant=mapping["tenant"])


def _review_profile_fields(actions: list[dict]) -> dict[str, object]:
    """Build exact expected Review values from semantic browser actions."""
    fields: dict[str, object] = {}
    for action in actions:
        operation = action.get("operation")
        selector = action.get("selector")
        value = action.get("value")
        if not isinstance(selector, str) or operation == "cdp_upload":
            continue
        if operation == "react_select_exact" and isinstance(value, dict):
            fields[selector] = value.get("selected_option") or value.get("exact_option")
        elif operation == "replace_tel_local_digits" and isinstance(value, str):
            fields[selector] = "".join(character for character in value if character.isdigit())
        else:
            fields[selector] = value
    return fields


def run_live_review(
    *,
    manifest_path: Path,
    approved_answers_path: Path,
    step: str,
    required_parser_repairs: list[str],
    required_question_ids: list[str],
    cdp_base_url: str,
    production_enabled: bool,
    transport_factory: LiveTransportFactory = ScopedCDPTransport,
    health_probe: LiveHealthProbe = browser_health.probe_cdp_health,
) -> tuple[dict, dict]:
    """Freshly observe Review and persist canonical, value-free authority evidence."""
    prepare_live_job._validate_local_cdp_base_url(cdp_base_url)
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    profile, resume = _verified_profile_resume(manifest)
    preparation = _load_runtime_object(
        manifest["runtime_paths"]["preparation"], label="preparation evidence"
    )
    identity = manifest["identity"]
    target = manifest["target"]
    mapping = tenant_field_maps.resolve_field_map(
        page_url=target["url"], platform=identity["platform"]
    )
    if mapping.get("tenant") != identity["tenant"]:
        raise OperatorBlockedError("learned tenant differs from manifest identity")
    live_run_manifest.validate_manifest(
        manifest,
        production_enabled=production_enabled,
        observed_binding={
            "target_id": preparation.get("target_id"),
            "page_url": preparation.get("page_url"),
            **(
                preparation.get("identity")
                if isinstance(preparation.get("identity"), dict)
                else {}
            ),
            "platform": preparation.get("platform"),
            "tenant": mapping["tenant"],
        },
    )
    approved_answers = _load_runtime_object(
        approved_answers_path, label="approved answers"
    )
    resume_answer = approved_answers.get("resume")
    if resume_answer is not None and Path(str(resume_answer)).resolve() != Path(
        resume["path"]
    ).resolve():
        raise OperatorBlockedError("approved resume differs from profile-selected Resume.pdf")
    approved_answers["resume"] = str(Path(resume["path"]).resolve())
    from schonfeld_form import URL as CLIENT_FORM_URL, verify_profile_answers
    if manifest["target"]["url"] == CLIENT_FORM_URL:
        verify_profile_answers(profile, approved_answers)
    else:
        from canonical_answers import verify_profile_answers as verify_canonical_answers
        verify_canonical_answers(profile, approved_answers, tenant=manifest["identity"]["tenant"])
    actions = tenant_field_maps.build_step_actions(
        mapping=mapping, step=step, approved_answers=approved_answers
    )
    _check_client_inputs(manifest, preparation, approved_answers)
    profile_fields = _review_profile_fields(actions)

    health = health_probe(cdp_base_url)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise OperatorBlockedError("loopback Chrome CDP health gate failed")
    transport = transport_factory(cdp_base_url)
    with transport.bind_mutable_page_target(target["id"]) as page:  # type: ignore[attr-defined]
        snapshot = page.read_only_snapshot()
        html = snapshot.get("html") if isinstance(snapshot, dict) else None
        if not isinstance(html, str):
            raise OperatorBlockedError("exact Review target HTML was unavailable")
        expected_identity = {
            key: identity[key] for key in ("company", "role", "requisition")
        }
        observed = prepare_live_job._dispatch_live_html(
            html_text=html,
            page_url=target["url"],
            expected_identity=expected_identity,
            expected_platform=identity["platform"],
            official_posting=snapshot.get("official_posting"),
        )
        live_run_manifest.validate_manifest(
            manifest,
            production_enabled=production_enabled,
            observed_binding={
                "target_id": snapshot.get("target_id"),
                "page_url": snapshot.get("url"),
                **{key: observed.get(key) for key in ("company", "role", "requisition")},
                "platform": observed.get("platform"),
                "tenant": mapping["tenant"],
            },
        )
        server_review = live_review_reader.read_server_review(
            page=page,
            platform=identity["platform"],
            mapping=mapping,
            step=step,
            target_id=target["id"],
            page_url=target["url"],
            identity=expected_identity,
            required_parser_repairs=required_parser_repairs,
            required_question_ids=required_question_ids,
        )

    if target["url"] != CLIENT_FORM_URL:
        profile_fields = _independent_review_fields(profile, mapping, step, server_review)
    authoritative = review_reconciler.reconcile_review(
        preparation_evidence=preparation,
        question_fields={name: control["selector"] for name, control in mapping["steps"][step]["controls"].items()},
        server_review=server_review,
        expected_target={
            "target_id": target["id"],
            "page_url": target["url"],
            **{key: identity[key] for key in ("company", "role", "requisition")},
        },
        profile_fields=profile_fields,
        resume_preflight={
            key: resume[key]
            for key in ("basename", "content_type", "sha256", "verified")
        },
        required_parser_repairs=required_parser_repairs,
        required_question_ids=required_question_ids,
    )
    wrapper = {
        "status": "reviewed" if authoritative["review_authoritative"] else "blocked",
        "job_identity": _manifest_job_identity(manifest),
        "review": authoritative,
    }
    output = Path(manifest["runtime_paths"]["review"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(wrapper, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return wrapper, _review_summary(wrapper)


def _write_protected_authorization_handoff(path: Path, issued: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise OperatorBlockedError("authorization handoff already exists")
    payload = {
        "schema_version": 1,
        "token": issued["token"],
        "actor": issued["binding"]["actor"],
        "review_evidence_sha256": issued["binding"]["review_evidence_sha256"],
        "expires_at": issued["expires_at"],
        "single_use": True,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        platform_io.make_private(path, descriptor)
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def run_live_authorize(
    *,
    manifest_path: Path,
    actor: str,
    approved_review_hash: str,
    expires_in_seconds: int,
    maango_approved: bool,
    production_enabled: bool,
    clock: LiveClock,
) -> dict[str, object]:
    """Issue one token and deliver it only through the protected runtime handoff."""
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    journal = Path(manifest["runtime_paths"]["submit_journal"])
    if journal.exists():
        raise OperatorBlockedError("existing submit intent journal forbids authorization replay")
    if manifest["mode"] == "production_live":
        from legacy_submission_migration import migrate_registered_history
        profile, _ = _verified_profile_resume(manifest)
        identity = _authorization_identity(manifest, profile)
        store = _authorization_store(manifest)
        key = store.ledger.application_key(**identity, job_id=manifest["job_id"],
                                           requisition=manifest["identity"]["requisition"])
        with store.ledger.transaction() as connection:
            # Legacy consumed tokens may predate account/tenant keys. They are
            # not proof of a new pre-intent attempt even with a missing journal.
            if connection.execute("""SELECT 1 FROM submission_authorizations
                    WHERE used_at IS NOT NULL AND (application_key = ? OR job_id = ?)""",
                    (key, manifest["job_id"])).fetchone():
                raise OperatorBlockedError("consumed authorization forbids authorization replay")
        history = migrate_registered_history(
            runtime_root=Path.home() / "Documents/job-agent/runtime",
            profile_path=manifest["profile"]["path"],
            account_id=identity["account_id"],
            ledger=store.ledger,
            current_pre_intent=(Path(manifest_path).absolute(), manifest),
        )
        if history["status"] == "blocked":
            raise OperatorBlockedError("registered historical evidence requires reconciliation before authorization")
        if _load_runtime_object(manifest_path, label="current pre-intent manifest") != manifest:
            raise OperatorBlockedError("current pre-intent manifest changed after validation")
    if not isinstance(actor, str) or not actor.strip():
        raise OperatorBlockedError("explicit authorization actor is required")
    if (
        not isinstance(expires_in_seconds, int)
        or isinstance(expires_in_seconds, bool)
        or not 1 <= expires_in_seconds <= 600
    ):
        raise OperatorBlockedError("authorization expiry must be between 1 and 600 seconds")
    _verify_employer_approval(manifest)
    gate_state = manifest["manual_gate"]
    if gate_state["gates"]:
        raise OperatorBlockedError("manual gates must be cleared before authorization")
    if gate_state["maango"] is True and not (
        gate_state["maango_approved"] is True and maango_approved is True
    ):
        raise OperatorBlockedError("explicit MAANGO approval is required")

    wrapper = _load_runtime_object(
        manifest["runtime_paths"]["review"], label="Review evidence"
    )
    if set(wrapper) != {"status", "job_identity", "review"}:
        raise OperatorBlockedError("canonical Review wrapper is required")
    if wrapper.get("status") != "reviewed" or wrapper.get("job_identity") != _manifest_job_identity(
        manifest
    ):
        raise OperatorBlockedError("Review job identity drift detected")
    review = wrapper.get("review")
    if not isinstance(review, dict):
        raise OperatorBlockedError("canonical Review evidence is required")
    review_hash = review.get("review_evidence_sha256")
    if (
        not isinstance(review_hash, str)
        or not isinstance(approved_review_hash, str)
        or not secrets.compare_digest(review_hash, approved_review_hash)
    ):
        raise OperatorBlockedError("explicit approved Review hash did not match")
    if (
        review.get("review_authoritative") is not True
        or review.get("submission_authorized") is not False
        or review.get("human_required") != []
    ):
        raise OperatorBlockedError("authoritative blocker-free Review is required")

    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise OperatorBlockedError("authorization clock must be timezone-aware")
    issued_at = now.astimezone(timezone.utc).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=expires_in_seconds)
    handoff_path = Path(manifest["runtime_paths"]["authorization_handoff"])
    if handoff_path.exists():
        raise OperatorBlockedError("authorization handoff already exists")
    store = _authorization_store(manifest)
    profile, _ = _verified_profile_resume(manifest)
    issued = store.issue(
        **_authorization_identity(manifest, profile),
        job_id=manifest["job_id"],
        review_evidence=review,
        actor=actor.strip(),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    _write_protected_authorization_handoff(handoff_path, issued)
    return {
        "status": "authorized",
        "authorization_issued": True,
        "single_use": True,
        "actor": actor.strip(),
        "expires_at": issued["expires_at"],
        "review_evidence_sha256": review_hash,
        "token_delivery": "protected_runtime_handoff",
        "job_identity": wrapper["job_identity"],
    }


def _read_protected_authorization_handoff(path: Path) -> dict:
    try:
        platform_io.refuse_symlink(path)
        descriptor = os.open(path, os.O_RDONLY | platform_io.NOFOLLOW)
    except OSError as exc:
        raise OperatorBlockedError("authorization handoff is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or not platform_io.is_private_file(path, descriptor)
            or metadata.st_size > 16384
        ):
            raise OperatorBlockedError("authorization handoff protection is invalid")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorBlockedError("authorization handoff is invalid") from exc
    expected_fields = {
        "schema_version",
        "token",
        "actor",
        "review_evidence_sha256",
        "expires_at",
        "single_use",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("schema_version") != 1
        or payload.get("single_use") is not True
        or not isinstance(payload.get("token"), str)
        or not payload["token"]
    ):
        raise OperatorBlockedError("authorization handoff is invalid")
    return payload


class _ManifestSubmitPage:
    """Revalidate exact handler identity on every one-shot page observation."""

    def __init__(self, *, page: object, manifest: dict, mapping: dict, production_enabled: bool):
        self.page = page
        self.manifest = manifest
        self.mapping = mapping
        self.production_enabled = production_enabled

    def read_only_snapshot(self) -> dict:
        snapshot = self.page.read_only_snapshot()  # type: ignore[attr-defined]
        html = snapshot.get("html") if isinstance(snapshot, dict) else None
        if not isinstance(html, str):
            raise OperatorBlockedError("exact submit target HTML was unavailable")
        identity = self.manifest["identity"]
        expected_identity = {
            key: identity[key] for key in ("company", "role", "requisition")
        }
        observed = prepare_live_job._dispatch_live_html(
            html_text=html,
            page_url=self.manifest["target"]["url"],
            expected_identity=expected_identity,
            expected_platform=identity["platform"],
            official_posting=snapshot.get("official_posting"),
        )
        live_run_manifest.validate_manifest(
            self.manifest,
            production_enabled=self.production_enabled,
            observed_binding={
                "target_id": snapshot.get("target_id"),
                "page_url": snapshot.get("url"),
                **{key: observed.get(key) for key in ("company", "role", "requisition")},
                "platform": observed.get("platform"),
                "tenant": self.mapping["tenant"],
            },
        )
        raw_gates = snapshot.get("gates", self.manifest["manual_gate"]["gates"])
        return {
            **snapshot,
            "identity": expected_identity,
            "gates": raw_gates,
            "maango": snapshot.get("maango", self.manifest["manual_gate"]["maango"]),
        }

    def inspect_submit_control(self, selector: str) -> dict:
        return self.page.inspect_submit_control(selector)  # type: ignore[attr-defined]

    def click_submit_once(self, selector: str) -> None:
        from schonfeld_form import URL
        after_reader = getattr(self.page, "read_after_submit_snapshot", None)
        if self.manifest["target"]["url"] != URL or not callable(after_reader):
            self.page.click_submit_once(selector)  # type: ignore[attr-defined]
            return
        # This capture exists only INSIDE the canonical invocation after intent.
        before = self.page.read_only_snapshot()
        intent = _require_submit_inspection_evidence(Path(self.manifest["runtime_paths"]["submit_journal"]), self.manifest)
        if intent.get("status") != "intent_recorded":
            raise OperatorBlockedError("original one-shot intent required for transition")
        try:
            self.page.click_submit_once(selector)
        finally:
            try:
                after = after_reader()
                if before.get("target_id") != after.get("target_id") or before.get("url") != URL:
                    raise ValueError("same-tab transition binding drift")
                transition = {
                    "source": "one_shot_same_tab_observation", "trusted": True,
                    "target_id": before["target_id"], "from_url": before["url"], "to_url": after["url"],
                    "intent_sha256": hashlib.sha256(json.dumps(intent,sort_keys=True,separators=(",", ":")).encode()).hexdigest(),
                    "before_html_sha256": hashlib.sha256(before["html"].encode()).hexdigest(),
                    "after_html_sha256": hashlib.sha256(after["html"].encode()).hexdigest(),
                    "after_body_text_sha256": hashlib.sha256(after["body_text"].encode()).hexdigest(),
                }
                path = Path(self.manifest["runtime_paths"]["confirmation"] + ".transition.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    platform_io.make_private(path, descriptor)
                    os.write(descriptor, json.dumps(transition,sort_keys=True).encode())
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except (OSError, ValueError, KeyError, TypeError):
                # Missing provenance is uncertain, never reconstructed from a URL.
                pass

    def inspect_confirmation(self) -> dict:
        return self.page.inspect_confirmation()  # type: ignore[attr-defined]


def run_live_submit(
    *,
    manifest_path: Path,
    approved_answers_path: Path,
    step: str,
    required_parser_repairs: list[str],
    required_question_ids: list[str],
    actor: str,
    maango_approved: bool,
    cdp_base_url: str,
    production_enabled: bool,
    transport_factory: LiveTransportFactory,
    health_probe: LiveHealthProbe,
    clock: LiveClock,
) -> dict[str, object]:
    """Recompute Review authority, consume once, journal, and click exactly once."""
    prepare_live_job._validate_local_cdp_base_url(cdp_base_url)
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    _verify_employer_approval(manifest)
    profile, resume = _verified_profile_resume(manifest)
    preparation = _load_runtime_object(
        manifest["runtime_paths"]["preparation"], label="preparation evidence"
    )
    wrapper = _load_runtime_object(
        manifest["runtime_paths"]["review"], label="Review evidence"
    )
    if (
        set(wrapper) != {"status", "job_identity", "review"}
        or wrapper.get("status") != "reviewed"
        or wrapper.get("job_identity") != _manifest_job_identity(manifest)
        or not isinstance(wrapper.get("review"), dict)
    ):
        raise OperatorBlockedError("canonical exact-job Review is required")
    persisted_review = wrapper["review"]
    persisted_hash = submission_authorization._review_hash(persisted_review)

    handoff_path = Path(manifest["runtime_paths"]["authorization_handoff"])
    handoff = _read_protected_authorization_handoff(handoff_path)
    if (
        handoff.get("actor") != actor
        or handoff.get("review_evidence_sha256") != persisted_hash
    ):
        raise OperatorBlockedError("authorization handoff binding drift detected")

    identity = manifest["identity"]
    target = manifest["target"]
    mapping = tenant_field_maps.resolve_field_map(
        page_url=target["url"], platform=identity["platform"]
    )
    if mapping.get("tenant") != identity["tenant"]:
        raise OperatorBlockedError("learned tenant differs from manifest identity")
    approved_answers = _load_runtime_object(
        approved_answers_path, label="approved answers"
    )
    resume_answer = approved_answers.get("resume")
    if resume_answer is not None and Path(str(resume_answer)).resolve() != Path(
        resume["path"]
    ).resolve():
        raise OperatorBlockedError("approved resume differs from profile-selected Resume.pdf")
    approved_answers["resume"] = str(Path(resume["path"]).resolve())
    from schonfeld_form import URL as CLIENT_FORM_URL, verify_profile_answers
    if manifest["target"]["url"] == CLIENT_FORM_URL:
        verify_profile_answers(profile, approved_answers)
    else:
        from canonical_answers import verify_profile_answers as verify_canonical_answers
        verify_canonical_answers(profile, approved_answers, tenant=manifest["identity"]["tenant"])
    actions = tenant_field_maps.build_step_actions(
        mapping=mapping, step=step, approved_answers=approved_answers
    )
    _check_client_inputs(manifest, preparation, approved_answers)
    profile_fields = _review_profile_fields(actions)
    review_step = mapping.get("steps", {}).get("review", {})
    submit_control = review_step.get("controls", {}).get("submit", {})
    if (
        not isinstance(submit_control, dict)
        or submit_control.get("operation") != "submit"
        or not isinstance(submit_control.get("selector"), str)
    ):
        raise OperatorBlockedError("exact learned submit control is unavailable")

    health = health_probe(cdp_base_url)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise OperatorBlockedError("loopback Chrome CDP health gate failed")
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise OperatorBlockedError("submission clock must be timezone-aware")

    transport = transport_factory(cdp_base_url)
    with transport.bind_mutable_page_target(target["id"]) as page:  # type: ignore[attr-defined]
        submit_page = _ManifestSubmitPage(
            page=page,
            manifest=manifest,
            mapping=mapping,
            production_enabled=production_enabled,
        )
        submit_page.read_only_snapshot()
        expected_identity = {
            key: identity[key] for key in ("company", "role", "requisition")
        }
        server_review = live_review_reader.read_server_review(
            page=page,
            platform=identity["platform"],
            mapping=mapping,
            step=step,
            target_id=target["id"],
            page_url=target["url"],
            identity=expected_identity,
            required_parser_repairs=required_parser_repairs,
            required_question_ids=required_question_ids,
        )
        if target["url"] != CLIENT_FORM_URL:
            profile_fields = _independent_review_fields(profile, mapping, step, server_review)
        fresh_review = review_reconciler.reconcile_review(
            preparation_evidence=preparation,
            question_fields={name: control["selector"] for name, control in mapping["steps"][step]["controls"].items()},
            server_review=server_review,
            expected_target={
                "target_id": target["id"],
                "page_url": target["url"],
                **expected_identity,
            },
            profile_fields=profile_fields,
            resume_preflight={
                key: resume[key]
                for key in ("basename", "content_type", "sha256", "verified")
            },
            required_parser_repairs=required_parser_repairs,
            required_question_ids=required_question_ids,
        )
        if (
            fresh_review.get("review_authoritative") is not True
            or not secrets.compare_digest(
                str(fresh_review.get("review_evidence_sha256", "")), persisted_hash
            )
        ):
            raise OperatorBlockedError("fresh authoritative Review hash drift detected")
        store = _authorization_store(manifest)
        submitted = one_shot_submit.execute_one_shot_submit(
            **_authorization_identity(manifest, profile),
            clock=clock,
            authorization_store=store,
            token=handoff["token"],
            page=submit_page,
            journal_path=Path(manifest["runtime_paths"]["submit_journal"]),
            job_id=manifest["job_id"],
            target_id=target["id"],
            expected_url=target["url"],
            requisition=identity["requisition"],
            review_evidence_sha256=persisted_hash,
            actor=actor,
            now=now,
            submit_selector=submit_control["selector"],
            maango_approved=maango_approved,
        )

    if submitted.get("authorization_consumed") is True:
        try:
            handoff_path.unlink()
        except OSError as exc:
            raise OperatorBlockedError(
                "authorization was consumed but protected handoff retirement failed"
            ) from exc
    result: dict[str, object] = {
        "status": submitted.get("status"),
        "stage": "confirmation_inspection",
        "authorization_consumed": submitted.get("authorization_consumed") is True,
        "one_shot": submitted.get("one_shot") is True,
        "replay_allowed": False,
        "confirmation_reconciled": False,
        "next_action": (
            "live confirmation"
            if submitted.get("status") == "confirmation_observed"
            else "inspect_confirmation_without_replay"
        ),
        "job_identity": wrapper["job_identity"],
    }
    if submitted.get("blocker"):
        result["blocker"] = submitted["blocker"]
    return result


def _require_submit_inspection_evidence(path: Path, manifest: dict) -> dict:
    try:
        entries = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise OperatorBlockedError("submit recovery journal is unavailable") from exc
    latest = entries[-1] if entries else None
    evidence = latest.get("evidence") if isinstance(latest, dict) else None
    if (
        not isinstance(latest, dict)
        or latest.get("action") != "submit"
        or not isinstance(evidence, dict)
        or evidence.get("job_id") != manifest["job_id"]
        or evidence.get("target_id") != manifest["target"]["id"]
        or evidence.get("page_url") != manifest["target"]["url"]
        or evidence.get("requisition") != manifest["identity"]["requisition"]
    ):
        raise OperatorBlockedError("exact submit inspection evidence is required")
    return evidence


def run_live_confirmation(
    *,
    manifest_path: Path,
    cdp_base_url: str,
    production_enabled: bool,
    transport_factory: LiveTransportFactory,
    health_probe: LiveHealthProbe,
    clock: LiveClock | None = None,
) -> tuple[dict, dict]:
    """Inspect confirmation and Candidate Home without navigating or replaying."""
    prepare_live_job._validate_local_cdp_base_url(cdp_base_url)
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    _require_submit_inspection_evidence(
        Path(manifest["runtime_paths"]["submit_journal"]), manifest
    )
    health = health_probe(cdp_base_url)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise OperatorBlockedError("loopback Chrome CDP health gate failed")
    target = manifest["target"]
    identity = manifest["identity"]
    transport = transport_factory(cdp_base_url)
    with transport.bind_mutable_page_target(target["id"]) as page:  # type: ignore[attr-defined]
        from schonfeld_form import URL
        context = {}
        if target["url"] == URL:
            review = _load_runtime_object(manifest["runtime_paths"]["review"], label="original Review")
            transition_path = Path(manifest["runtime_paths"]["confirmation"] + ".transition.json")
            transition = _load_runtime_object(transition_path, label="one-shot transition") if transition_path.exists() else {}
            context["guest_submission"] = {
                "job_id": manifest["job_id"], "journal_path": manifest["runtime_paths"]["submit_journal"],
                "review_evidence": review.get("review"), "transition": transition,
            }
        portal = live_confirmation_reader.read_and_reconcile(
            **context,
            page=page,
            platform=identity["platform"],
            tenant=identity["tenant"],
            target_id=target["id"],
            expected_url=target["url"],
            expected_identity={
                key: identity[key] for key in ("company", "role", "requisition")
            },
        )
    wrapper = {
        "status": "portal_confirmed" if portal["portal_confirmed"] else "human_required",
        "job_identity": _manifest_job_identity(manifest),
        "portal": portal,
    }
    output = Path(manifest["runtime_paths"]["confirmation"])
    from autonomous_controller import atomic_json
    atomic_json(output, wrapper)
    if portal.get("portal_confirmed") is True and portal.get("safe_for_post_submit") is True:
        store = _authorization_store(manifest)
        profile = _load_runtime_object(manifest["profile"]["path"], label="canonical account profile")
        attempt = store.ledger.attempt_for_application(
            job_id=manifest["job_id"], requisition=identity["requisition"],
            **_authorization_identity(manifest, profile))
        if attempt is not None:
            store.ledger.confirm(attempt["submission_attempt_id"], confirmed=True,
                                 now=(clock or _utc_now)())
    summary = {
        "status": wrapper["status"],
        "portal_confirmed": portal.get("portal_confirmed") is True,
        "safe_for_post_submit": portal.get("safe_for_post_submit") is True,
        "matched_application_count": portal.get("portal_readback", {}).get(
            "matched_application_count", 0
        ),
        "human_required": portal.get("human_required", []),
        "reader": portal.get("reader"),
        "job_identity": wrapper["job_identity"],
    }
    return wrapper, summary


def run_live_delivery(
    *,
    manifest_path: Path,
    submitted_date: str,
    production_enabled: bool,
    commit_external: bool,
    discord_channel_id: str | None,
    discord_token_env: str,
    tracker_adapter: object | None,
    discord_adapter: object | None,
    tracker_sync: bool = False,
) -> dict[str, object]:
    """Persist confirmed outcome and notify; Sheets is an explicit opt-in."""
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    try:
        datetime.strptime(submitted_date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise OperatorBlockedError("submitted date must be explicit YYYY-MM-DD") from exc
    confirmation = _load_runtime_object(
        manifest["runtime_paths"]["confirmation"], label="confirmation evidence"
    )
    if (
        set(confirmation) != {"status", "job_identity", "portal"}
        or confirmation.get("status") != "portal_confirmed"
        or confirmation.get("job_identity") != _manifest_job_identity(manifest)
        or not isinstance(confirmation.get("portal"), dict)
    ):
        raise OperatorBlockedError("exact portal-confirmed artifact is required")

    if tracker_adapter is not None and tracker_sync is not True:
        raise OperatorBlockedError("explicit tracker synchronization opt-in is required")
    if manifest["mode"] == "production_live":
        if commit_external is not True:
            raise OperatorBlockedError("explicit external commit mode is required")
        if tracker_sync is True and tracker_adapter is None:
            tracker_adapter = live_delivery_adapters.GoogleSheetsTransactionAdapter(
                commit_mode=live_delivery_adapters.COMMIT_EXTERNAL
            )
        if discord_adapter is None:
            if not isinstance(discord_channel_id, str) or not discord_channel_id:
                raise OperatorBlockedError("explicit Discord channel ID is required")
            if not isinstance(discord_token_env, str) or not discord_token_env:
                raise OperatorBlockedError("Discord runtime token environment name is required")
            client = live_delivery_adapters.DiscordRESTClient(
                token_provider=lambda: os.environ.get(discord_token_env, "")
            )
            discord_adapter = live_delivery_adapters.DiscordTransactionAdapter(
                commit_mode=live_delivery_adapters.COMMIT_EXTERNAL,
                channel_id=discord_channel_id,
                client=client,
            )
        delivery_mode = "external_commit"
    else:
        if commit_external is True:
            raise OperatorBlockedError("external commit is forbidden for sanitized manifests")
        if tracker_sync is True:
            tracker_adapter = tracker_adapter or _TimedLocalTracker()
        discord_adapter = discord_adapter or _TimedLocalDiscord()
        delivery_mode = "sanitized_local"

    identity = manifest["identity"]
    tracker_payload = {
        "company": identity["company"],
        "status": "Submitted - Pending Response",
        "role": identity["role"],
        "salary": "",
        "date_submitted": submitted_date,
        "job_url": manifest["target"]["url"],
        "rejection_reason": "N/A",
        "notes": f"Verified portal confirmation for requisition {identity['requisition']}",
    }
    discord_message = notifier.build_message(
        "applied",
        company=identity["company"],
        role=identity["role"],
        url=manifest["target"]["url"],
        detail=f"Verified requisition {identity['requisition']}",
    )
    transaction = post_submit_transaction.PostSubmitTransactionCoordinator(
        state_path=manifest["runtime_paths"]["transaction_db"],
        tracker=tracker_adapter,  # type: ignore[arg-type]
        discord=discord_adapter,  # type: ignore[arg-type]
    ).run(
        job_id=manifest["job_id"],
        portal_evidence=confirmation["portal"],
        tracker_payload=tracker_payload if tracker_sync is True else None,
        discord_message=discord_message,
    )
    return {**transaction, "delivery_mode": delivery_mode}


def _json_artifact_state(path: Path, *, complete_status: str) -> str:
    if not path.is_file():
        return "not_started"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    if not isinstance(payload, dict):
        return "invalid"
    status_value = payload.get("status")
    if status_value == complete_status:
        return "complete"
    if status_value in {"blocked", "human_required"}:
        return "human_required"
    if complete_status == "prepared" and payload.get("review_ready") is True:
        return "complete"
    return "invalid"


def _submit_journal_state(path: Path, manifest: dict) -> str:
    if not path.is_file():
        return "not_started"
    try:
        entries = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError):
        return "invalid"
    latest = entries[-1] if entries else None
    evidence = latest.get("evidence") if isinstance(latest, dict) else None
    if (
        not isinstance(latest, dict)
        or latest.get("action") != "submit"
        or not isinstance(evidence, dict)
        or evidence.get("job_id") != manifest["job_id"]
        or evidence.get("target_id") != manifest["target"]["id"]
        or evidence.get("page_url") != manifest["target"]["url"]
        or evidence.get("requisition") != manifest["identity"]["requisition"]
    ):
        return "invalid"
    return "confirmation_observed" if evidence.get("verified") is True else "uncertain"


def build_live_status(manifest: dict) -> dict[str, object]:
    """Derive a value-free recovery decision from manifest-bound durable evidence."""
    paths = manifest["runtime_paths"]
    preparation_state = _json_artifact_state(
        Path(paths["preparation"]), complete_status="prepared"
    )
    review_state = _json_artifact_state(Path(paths["review"]), complete_status="reviewed")
    submit_state = _submit_journal_state(Path(paths["submit_journal"]), manifest)
    confirmation_state = _json_artifact_state(
        Path(paths["confirmation"]), complete_status="portal_confirmed"
    )
    handoff = Path(paths["authorization_handoff"])
    if submit_state != "not_started":
        authorization_state = "consumed_or_unavailable"
    elif handoff.is_file() and platform_io.is_private_file(handoff):
        authorization_state = "available"
    elif Path(paths["authorization_db"]).is_file():
        authorization_state = "handoff_missing"
    else:
        authorization_state = "not_started"
    transaction = post_submit_transaction.inspect_transaction_state(
        paths["transaction_db"], job_id=manifest["job_id"]
    )
    stages = {
        "preparation": preparation_state,
        "review": review_state,
        "authorization": authorization_state,
        "submit": submit_state,
        "confirmation": confirmation_state,
        "tracker": transaction["tracker"],
        "discord": transaction["discord"],
    }
    if transaction["status"] == "complete":
        next_action = "complete"
    elif transaction["tracker"] == "readback_pending" or transaction["discord"] == "readback_pending":
        next_action = "resume_delivery_readback"
    elif confirmation_state == "complete":
        next_action = "live deliver"
    elif confirmation_state in {"human_required", "invalid"}:
        next_action = "human_required"
    elif submit_state in {"uncertain", "confirmation_observed"}:
        next_action = "inspect_confirmation_without_replay"
    elif submit_state == "invalid" or authorization_state == "handoff_missing":
        next_action = "human_required"
    elif authorization_state == "available":
        next_action = "live submit"
    elif review_state == "complete":
        next_action = "live authorize"
    elif review_state in {"human_required", "invalid"}:
        next_action = "human_required"
    elif preparation_state == "complete":
        next_action = "live review"
    elif preparation_state in {"human_required", "invalid"}:
        next_action = "human_required"
    else:
        next_action = "live prepare"
    report = {
        "schema_version": 1,
        "status": "complete" if next_action == "complete" else "incomplete",
        "job_identity": _manifest_job_identity(manifest),
        "stages": stages,
        "next_action": next_action,
        "submit_replay_allowed": False,
    }
    output = Path(paths["status"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def run_live_status(*, manifest_path: Path, production_enabled: bool) -> dict[str, object]:
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    return build_live_status(manifest)


def run_normal_chrome_preflight(
    *,
    manifest_path: Path,
    cdp_base_url: str,
    production_enabled: bool,
    transport_factory: LiveReadOnlyTransportFactory,
    health_probe: LiveHealthProbe,
) -> dict[str, object]:
    """Prove exact-target normal-Chrome attachment through read-only snapshots."""
    prepare_live_job._validate_local_cdp_base_url(cdp_base_url)
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    health = health_probe(cdp_base_url)
    if not isinstance(health, dict) or health.get("status") != "ready":
        raise OperatorBlockedError("loopback Chrome CDP health gate failed")
    target = manifest["target"]
    identity = manifest["identity"]
    transport = transport_factory(cdp_base_url)
    with transport.bind_page_target(target["id"]) as page:  # type: ignore[attr-defined]
        before = page.read_only_snapshot()
        after = page.read_only_snapshot()
    for snapshot in (before, after):
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("read_only") is not True
            or snapshot.get("target_id") != target["id"]
            or snapshot.get("url") != target["url"]
            or not isinstance(snapshot.get("html"), str)
        ):
            raise OperatorBlockedError("read-only exact-target preflight drift detected")
    fingerprint_keys = ("target_id", "url", "title", "body_text", "html")
    before_fingerprint = hashlib.sha256(
        json.dumps(
            {key: before.get(key) for key in fingerprint_keys},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    after_fingerprint = hashlib.sha256(
        json.dumps(
            {key: after.get(key) for key in fingerprint_keys},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if not secrets.compare_digest(before_fingerprint, after_fingerprint):
        raise OperatorBlockedError("page content changed during read-only preflight")
    expected_identity = {
        key: identity[key] for key in ("company", "role", "requisition")
    }
    observed = prepare_live_job._dispatch_live_html(
        html_text=before["html"],
        page_url=target["url"],
        expected_identity=expected_identity,
        expected_platform=identity["platform"],
        official_posting=before.get("official_posting"),
    )
    live_run_manifest.validate_manifest(
        manifest,
        production_enabled=production_enabled,
        observed_binding={
            "target_id": before["target_id"],
            "page_url": before["url"],
            **{key: observed.get(key) for key in ("company", "role", "requisition")},
            "platform": observed.get("platform"),
            "tenant": identity["tenant"],
        },
    )
    return {
        "status": "ready",
        "exact_target_attached": True,
        "read_only": True,
        "content_unchanged": True,
        "identity_verified": True,
        "submission_enabled": False,
        "job_identity": _manifest_job_identity(manifest),
    }


def run_live_resume(
    *,
    manifest_path: Path,
    cdp_base_url: str,
    submitted_date: str | None,
    production_enabled: bool,
    commit_external: bool,
    discord_channel_id: str | None,
    discord_token_env: str,
    transport_factory: LiveTransportFactory,
    health_probe: LiveHealthProbe,
    tracker_adapter: object | None,
    discord_adapter: object | None,
    tracker_sync: bool = False,
) -> dict[str, object]:
    """Resume only confirmation inspection or claimed downstream read-back."""
    manifest = live_run_manifest.load_manifest(
        manifest_path, production_enabled=production_enabled
    )
    status_report = build_live_status(manifest)
    if status_report["next_action"] == "inspect_confirmation_without_replay":
        _, summary = run_live_confirmation(
            manifest_path=manifest_path,
            cdp_base_url=cdp_base_url,
            production_enabled=production_enabled,
            transport_factory=transport_factory,
            health_probe=health_probe,
        )
        return {
            **summary,
            "resume_action": "confirmation_inspection_without_replay",
            "submit_replay_allowed": False,
        }
    if status_report["next_action"] == "resume_delivery_readback":
        if not isinstance(submitted_date, str) or not submitted_date:
            raise OperatorBlockedError("submitted date is required for delivery read-back")
        delivery = run_live_delivery(
            manifest_path=manifest_path,
            submitted_date=submitted_date,
            production_enabled=production_enabled,
            commit_external=commit_external,
            discord_channel_id=discord_channel_id,
            discord_token_env=discord_token_env,
            tracker_adapter=tracker_adapter,
            discord_adapter=discord_adapter,
            tracker_sync=tracker_sync,
        )
        return {
            **delivery,
            "resume_action": "delivery_readback_without_replay",
            "submit_replay_allowed": False,
        }
    return {
        **status_report,
        "resume_action": "none",
        "resume_performed": False,
    }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main(
    argv: list[str] | None = None,
    *,
    demo_runner: DemoRunner | None = None,
    live_transport_factory: LiveTransportFactory = ScopedCDPTransport,
    live_readonly_transport_factory: LiveReadOnlyTransportFactory = ScopedCDPTransport,
    live_health_probe: LiveHealthProbe = browser_health.probe_cdp_health,
    live_coverage: LiveCoverage = prepare_live_job.build_coverage_matrix,
    live_clock: LiveClock = _utc_now,
    live_tracker_adapter: object | None = None,
    live_discord_adapter: object | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Guarded production operator proof")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("local-demo")
    demo.add_argument("--resume", required=True)
    demo.add_argument("--runtime-dir", required=True)
    demo.add_argument("--output", required=True)
    demo.add_argument("--approve-sanitized-submit", action="store_true")
    audit = commands.add_parser("audit")
    audit.add_argument("--report", required=True)
    live = commands.add_parser("live")
    live_commands = live.add_subparsers(dest="live_command", required=True)
    live_prepare = live_commands.add_parser("prepare")
    live_prepare.add_argument("--manifest", required=True)
    live_prepare.add_argument("--approved-answers", required=True)
    live_prepare.add_argument("--step", required=True)
    live_prepare.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    live_prepare.add_argument("--enable-production-live", action="store_true")
    live_review = live_commands.add_parser("review")
    live_review.add_argument("--manifest", required=True)
    live_review.add_argument("--approved-answers", required=True)
    live_review.add_argument("--step", required=True)
    live_review.add_argument("--required-parser-repair", action="append", default=[])
    live_review.add_argument("--required-question", action="append", default=[])
    live_review.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    live_review.add_argument("--enable-production-live", action="store_true")
    live_authorize = live_commands.add_parser("authorize")
    live_authorize.add_argument("--manifest", required=True)
    live_authorize.add_argument("--actor", required=True)
    live_authorize.add_argument("--approve-review-hash", required=True)
    live_authorize.add_argument("--expires-in-seconds", required=True, type=int)
    live_authorize.add_argument("--approve-maango", action="store_true")
    live_authorize.add_argument("--enable-production-live", action="store_true")
    live_submit = live_commands.add_parser("submit")
    live_submit.add_argument("--manifest", required=True)
    live_submit.add_argument("--approved-answers", required=True)
    live_submit.add_argument("--step", required=True)
    live_submit.add_argument("--required-parser-repair", action="append", default=[])
    live_submit.add_argument("--required-question", action="append", default=[])
    live_submit.add_argument("--actor", required=True)
    live_submit.add_argument("--approve-maango", action="store_true")
    live_submit.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    live_submit.add_argument("--enable-production-live", action="store_true")
    live_confirmation = live_commands.add_parser("confirmation")
    live_confirmation.add_argument("--manifest", required=True)
    live_confirmation.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    live_confirmation.add_argument("--enable-production-live", action="store_true")
    live_deliver = live_commands.add_parser("deliver")
    live_deliver.add_argument("--manifest", required=True)
    live_deliver.add_argument("--submitted-date", required=True)
    live_deliver.add_argument("--commit-external", action="store_true")
    live_deliver.add_argument("--sync-tracker", action="store_true")
    live_deliver.add_argument("--discord-channel-id")
    live_deliver.add_argument("--discord-token-env", default="JOB_AGENT_DISCORD_BOT_TOKEN")
    live_deliver.add_argument("--enable-production-live", action="store_true")
    live_status = live_commands.add_parser("status")
    live_status.add_argument("--manifest", required=True)
    live_status.add_argument("--enable-production-live", action="store_true")
    live_resume = live_commands.add_parser("resume")
    live_resume.add_argument("--manifest", required=True)
    live_resume.add_argument("--submitted-date")
    live_resume.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    live_resume.add_argument("--commit-external", action="store_true")
    live_resume.add_argument("--sync-tracker", action="store_true")
    live_resume.add_argument("--discord-channel-id")
    live_resume.add_argument("--discord-token-env", default="JOB_AGENT_DISCORD_BOT_TOKEN")
    live_resume.add_argument("--enable-production-live", action="store_true")
    live_preflight = live_commands.add_parser("preflight")
    live_preflight.add_argument("--manifest", required=True)
    live_preflight.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    live_preflight.add_argument("--enable-production-live", action="store_true")
    live_release_audit = live_commands.add_parser("release-audit")
    live_release_audit.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)

    if args.command == "live":
        try:
            if args.live_command == "prepare":
                result = run_live_prepare(
                    manifest_path=Path(args.manifest),
                    approved_answers_path=Path(args.approved_answers),
                    step=args.step,
                    cdp_base_url=args.cdp_base_url,
                    production_enabled=args.enable_production_live,
                    transport_factory=live_transport_factory,
                    health_probe=live_health_probe,
                    coverage=live_coverage,
                )
                exit_code = 0
            elif args.live_command == "review":
                wrapper, result = run_live_review(
                    manifest_path=Path(args.manifest),
                    approved_answers_path=Path(args.approved_answers),
                    step=args.step,
                    required_parser_repairs=args.required_parser_repair,
                    required_question_ids=args.required_question,
                    cdp_base_url=args.cdp_base_url,
                    production_enabled=args.enable_production_live,
                    transport_factory=live_transport_factory,
                    health_probe=live_health_probe,
                )
                exit_code = 0 if wrapper["review"]["review_authoritative"] else 1
            elif args.live_command == "authorize":
                result = run_live_authorize(
                    manifest_path=Path(args.manifest),
                    actor=args.actor,
                    approved_review_hash=args.approve_review_hash,
                    expires_in_seconds=args.expires_in_seconds,
                    maango_approved=args.approve_maango,
                    production_enabled=args.enable_production_live,
                    clock=live_clock,
                )
                exit_code = 0
            elif args.live_command == "submit":
                result = run_live_submit(
                    manifest_path=Path(args.manifest),
                    approved_answers_path=Path(args.approved_answers),
                    step=args.step,
                    required_parser_repairs=args.required_parser_repair,
                    required_question_ids=args.required_question,
                    actor=args.actor,
                    maango_approved=args.approve_maango,
                    cdp_base_url=args.cdp_base_url,
                    production_enabled=args.enable_production_live,
                    transport_factory=live_transport_factory,
                    health_probe=live_health_probe,
                    clock=live_clock,
                )
                exit_code = 0 if result.get("status") == "confirmation_observed" else 1
            elif args.live_command == "confirmation":
                wrapper, result = run_live_confirmation(
                    manifest_path=Path(args.manifest),
                    cdp_base_url=args.cdp_base_url,
                    production_enabled=args.enable_production_live,
                    transport_factory=live_transport_factory,
                    health_probe=live_health_probe,
                )
                exit_code = 0 if wrapper["portal"]["portal_confirmed"] else 1
            elif args.live_command == "deliver":
                result = run_live_delivery(
                    manifest_path=Path(args.manifest),
                    submitted_date=args.submitted_date,
                    production_enabled=args.enable_production_live,
                    commit_external=args.commit_external,
                    discord_channel_id=args.discord_channel_id,
                    discord_token_env=args.discord_token_env,
                    tracker_adapter=live_tracker_adapter,
                    discord_adapter=live_discord_adapter,
                    tracker_sync=args.sync_tracker,
                )
                exit_code = 0 if result.get("status") == "complete" else 1
            elif args.live_command == "status":
                result = run_live_status(
                    manifest_path=Path(args.manifest),
                    production_enabled=args.enable_production_live,
                )
                exit_code = 0
            elif args.live_command == "resume":
                result = run_live_resume(
                    manifest_path=Path(args.manifest),
                    cdp_base_url=args.cdp_base_url,
                    submitted_date=args.submitted_date,
                    production_enabled=args.enable_production_live,
                    commit_external=args.commit_external,
                    discord_channel_id=args.discord_channel_id,
                    discord_token_env=args.discord_token_env,
                    transport_factory=live_transport_factory,
                    health_probe=live_health_probe,
                    tracker_adapter=live_tracker_adapter,
                    discord_adapter=live_discord_adapter,
                    tracker_sync=args.sync_tracker,
                )
                exit_code = 1 if result.get("status") == "partial" else 0
            elif args.live_command == "preflight":
                result = run_normal_chrome_preflight(
                    manifest_path=Path(args.manifest),
                    cdp_base_url=args.cdp_base_url,
                    production_enabled=args.enable_production_live,
                    transport_factory=live_readonly_transport_factory,
                    health_probe=live_health_probe,
                )
                exit_code = 0
            else:
                result = run_live_release_audit(evidence_path=Path(args.evidence))
                exit_code = 0 if result["checks_passed"] is True else 1
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({
                "error": str(exc),
                "stage": args.live_command,
                "status": "blocked",
                "submission_enabled": False,
                "review_evidence_persisted": False,
            }, sort_keys=True))
            return 2
        print(json.dumps(result, sort_keys=True))
        return exit_code

    if args.command == "audit":
        try:
            payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = {
                "checks_passed": False,
                "real_application_authorized": False,
                "status": "not_ready",
            }
            print(json.dumps(result, sort_keys=True))
            return 2
        result = audit_operator_report(payload)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["checks_passed"] is True else 1

    if args.approve_sanitized_submit is not True:
        print(json.dumps({
            "error": "explicit sanitized-demo submit approval is required",
            "real_application_authorized": False,
            "report_persisted": False,
        }, sort_keys=True))
        return 2

    runner = demo_runner or run_local_sanitized_demo
    try:
        report = runner(
            resume_path=Path(args.resume),
            runtime_dir=Path(args.runtime_dir),
            approved=True,
        )
        if audit_operator_report(report).get("checks_passed") is not True:
            raise OperatorBlockedError("final sanitized operator audit failed")
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({
            "error": str(exc),
            "real_application_authorized": False,
            "report_persisted": False,
        }, sort_keys=True))
        return 2

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
