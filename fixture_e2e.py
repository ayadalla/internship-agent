"""Non-submitting end-to-end fixture flow for ATS handler safety contracts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from answer_coverage import build_coverage_matrix
from app_queue import ApplicationQueue
from execution_journal import ExecutionJournal
from njoyn_handler import inspect_html as inspect_njoyn_html
from queue_worker import resume_or_prepare_leased_job
from submission_artifacts import build_submission_artifact


def run_fixture_flow(
    *,
    fixture_path: Path,
    candidate: dict[str, str],
    profile: dict,
    questions: list[dict],
    expected_resume_basename: str,
    queue_db_path: Path,
    plan_dir: Path,
    confirmation_url: str,
    confirmation_text: str,
) -> dict[str, Any]:
    """Exercise discovery through evidence construction without external mutation."""
    queue = ApplicationQueue(Path(queue_db_path))
    queued = queue.enqueue(
        company=candidate["company"],
        role=candidate["role"],
        url=candidate["url"],
        ats_platform=candidate["ats_platform"],
    )
    coverage = build_coverage_matrix(
        profile=profile,
        questions=questions,
        company=candidate["company"],
    )
    leased = queue.lease_next(now="2026-08-25T00:00:00+00:00", lease_seconds=300)
    if leased is None:
        raise ValueError("Fixture candidate was not available for preparation")
    journal = ExecutionJournal(Path(plan_dir) / "fixture-e2e-journal.jsonl")
    prepared = resume_or_prepare_leased_job(
        queue=queue,
        leased_job=leased,
        journal=journal,
        html_text=Path(fixture_path).read_text(encoding="utf-8"),
        expected_resume_basename=expected_resume_basename,
        now="2026-08-25T00:00:01+00:00",
        plan_dir=Path(plan_dir),
    )
    plan = json.loads(Path(prepared["plan_path"]).read_text(encoding="utf-8"))
    artifact = build_submission_artifact(
        candidate,
        confirmation_url=confirmation_url,
        confirmation_text=confirmation_text,
    )
    return {
        "submission_enabled": False,
        "source_candidate": dict(candidate),
        "queue_job": prepared["queue_job"],
        "answer_coverage": coverage,
        "plan": plan,
        "evidence_artifact": artifact,
    }


def run_workday_fixture_flow(**kwargs: Any) -> dict[str, Any]:
    """Run the shared non-submitting E2E contract against a Workday fixture."""
    return run_fixture_flow(**kwargs)


def run_lever_fixture_flow(**kwargs: Any) -> dict[str, Any]:
    """Run the shared non-submitting E2E contract against a Lever fixture."""
    return run_fixture_flow(**kwargs)


def run_oracle_fixture_flow(**kwargs: Any) -> dict[str, Any]:
    """Run the shared non-submitting E2E contract against an Oracle fixture."""
    return run_fixture_flow(**kwargs)


def run_njoyn_fixture_flow(
    *,
    parser_fixture_path: Path,
    parser_corrections: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run CGI/Njoyn preparation with resume and parser-review evidence."""
    result = run_fixture_flow(**kwargs)
    parser_review = inspect_njoyn_html(
        Path(parser_fixture_path).read_text(encoding="utf-8"),
        page_url=kwargs["candidate"]["url"],
    )
    required_corrections = parser_review["parser_mismatches"]
    recorded_corrections = parser_corrections or []
    resolved_corrections = [
        field for field in required_corrections if field in recorded_corrections
    ]
    unresolved_corrections = [
        field for field in required_corrections if field not in recorded_corrections
    ]
    rejected_corrections = [
        field for field in recorded_corrections if field not in required_corrections
    ]
    result["parser_review"] = parser_review
    repair_evidence = {
        "status": (
            "not_required"
            if not required_corrections
            else "resolved"
            if not unresolved_corrections
            else "blocked"
        ),
        "required_corrections": required_corrections,
        "resolved_corrections": resolved_corrections,
        "unresolved_corrections": unresolved_corrections,
        "safe_to_continue": not unresolved_corrections,
    }
    if rejected_corrections:
        repair_evidence["rejected_corrections"] = rejected_corrections
    result["parser_repair_evidence"] = repair_evidence
    return result
