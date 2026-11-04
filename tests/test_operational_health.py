from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_queue
import operational_health
import pytest


@pytest.mark.parametrize("dependency,value", [
    ("source_report", {"source_health_status": "failed"}),
    ("browser_report", {"status": "error"}),
    ("browser_report", {"status": "ready", "verified": False}),
    ("browser_report", {"status": "ready"}),
    ("oauth_ready", False),
    ("notification_report", {"status": "failed", "verified": False}),
    ("notification_report", {"status": "delivered", "verified": False}),
])
def test_required_failure_never_reports_ready(tmp_path, dependency, value):
    evidence = dict(source_report={"source_health_status": "healthy"},
                    browser_report={"status": "ready", "verified": True}, oauth_ready=True,
                    queue=app_queue.ApplicationQueue(tmp_path / "queue.db"),
                    tracker_reconciliation={"drifts": []},
                    notification_report={"status": "delivered", "verified": True})
    evidence[dependency] = value
    assert operational_health.build_report(**evidence)["status"] == "degraded"


def test_oauth_and_tracker_are_optional_unless_explicitly_required(tmp_path):
    evidence = dict(source_report={"source_health_status": "healthy"},
                    browser_report={"status": "ready", "verified": True}, oauth_ready=False,
                    queue=app_queue.ApplicationQueue(tmp_path / "queue.db"),
                    notification_report={"status": "delivered", "verified": True})
    report = operational_health.build_report(**evidence)
    assert report["status"] == "ready"
    assert report["tracker"]["status"] == "not_requested"
    assert report["dependencies"] == {
        "required": ["sources", "browser", "notifications"],
        "optional": ["oauth", "tracker"], "blocked_by": []}
    report = operational_health.build_report(**evidence, oauth_required=True)
    assert report["status"] == "degraded"
    assert report["dependencies"]["blocked_by"] == ["oauth"]


def test_build_report_summarizes_all_required_operational_surfaces(tmp_path):
    queue = app_queue.ApplicationQueue(tmp_path / "queue.db")
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    leased = queue.lease_next(now="2026-08-25T08:00:00+00:00", lease_seconds=300)
    assert leased is not None

    report = operational_health.build_report(
        source_report={"source_health_status": "healthy"},
        browser_report={"status": "ready", "recoverable": False, "verified": True},
        oauth_ready=True,
        queue=queue,
        tracker_reconciliation={"drifts": [{"job_id": 99}], "mutations": []},
        notification_report={"status": "delivered", "verified": True},
    )

    assert report == {
        "status": "degraded",
        "dependencies": {"required": ["sources", "browser", "notifications", "oauth", "tracker"],
                         "optional": [], "blocked_by": ["tracker"]},
        "sources": {"status": "healthy"},
        "browser": {"status": "ready", "recoverable": False, "verified": True},
        "oauth": {"status": "ready"},
        "queue": {"leased_count": 1, "expired_lease_count": 0},
        "tracker": {"drift_count": 1, "status": "drift_detected"},
        "notifications": {"status": "delivered", "verified": True},
    }


@pytest.mark.parametrize("browser_ok,oauth_required,oauth_ok,expected", [
    (True, False, False, 0), (False, False, False, 1), (True, True, False, 1),
])
def test_main_no_implicit_sheets_or_token_readiness(tmp_path, monkeypatch, capsys,
                                                  browser_ok, oauth_required, oauth_ok, expected):
    import json
    source = tmp_path / "sources.json"
    source.write_text('{"source_health_status":"healthy"}', encoding="utf-8")
    notification = tmp_path / "notifications.json"
    notification.write_text('{"status":"delivered","verified":true}', encoding="utf-8")
    token = tmp_path / "token.json"
    token.write_text('{}', encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail("implicit external probe")
    monkeypatch.setattr(operational_health.tracker, "fetch_rows_via_api", forbidden)
    monkeypatch.setattr(operational_health.browser_health, "probe_cdp_health", forbidden)
    argv = ["--source-report", str(source), "--queue-db", str(tmp_path / "queue.db"),
            "--notification-report", str(notification), "--oauth-token", str(token)]
    if oauth_required:
        argv.append("--require-oauth")
    assert operational_health.main(argv,
        browser_caller=lambda: {"status": "ready" if browser_ok else "error", "verified": browser_ok},
        oauth_probe=lambda: oauth_ok) == expected
    report = json.loads(capsys.readouterr().out)
    assert report["tracker"]["status"] == "not_requested"
    assert report["oauth"]["status"] != "ready"


def test_explicit_tracker_probe_failure_is_reported_not_hidden_as_in_sync(tmp_path, monkeypatch, capsys):
    import json
    source = tmp_path / "sources.json"
    source.write_text('{"source_health_status":"healthy"}', encoding="utf-8")
    def fail():
        raise TimeoutError("private upstream diagnostic")
    monkeypatch.setattr(operational_health.tracker, "fetch_rows_via_api", fail)
    result = operational_health.main(["--source-report", str(source), "--check-tracker",
                                      "--queue-db", str(tmp_path / "queue.db")],
        browser_caller=lambda: {"status": "ready", "verified": True}, oauth_probe=lambda: True)
    assert result == 1
    report = json.loads(capsys.readouterr().out)
    assert report["tracker"]["status"] == "unavailable"
    assert "tracker" in report["dependencies"]["blocked_by"]
    assert "private upstream diagnostic" not in json.dumps(report)


def test_main_runs_read_only_probes_and_emits_machine_readable_report(tmp_path, monkeypatch, capsys):
    queue_path = tmp_path / "queue.db"
    queue = app_queue.ApplicationQueue(queue_path)
    queue.enqueue(
        company="Example",
        role="Software Engineer Intern",
        url="https://job-boards.greenhouse.io/example/jobs/123",
        ats_platform="Greenhouse",
    )
    source_report = tmp_path / "sources-report.json"
    source_report.write_text('{"source_health_status": "healthy"}', encoding="utf-8")
    tracker_rows = tmp_path / "tracker-rows.json"
    tracker_rows.write_text('[]', encoding="utf-8")
    notification_report = tmp_path / "notification-report.json"
    notification_report.write_text('{"status": "delivered", "verified": true}', encoding="utf-8")
    oauth_token = tmp_path / "oauth.json"
    oauth_token.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        operational_health.browser_health,
        "probe_cdp_health",
        lambda url: {"status": "ready", "recoverable": False, "base_url": url},
    )

    assert operational_health.main([
        "--source-report", str(source_report),
        "--queue-db", str(queue_path),
        "--tracker-rows", str(tracker_rows),
        "--notification-report", str(notification_report),
        "--oauth-token", str(oauth_token),
        "--browser-base-url", "http://browser.test:18800",
    ], browser_caller=lambda: {"status": "ready", "verified": True},
       oauth_probe=lambda: True) == 0

    report = __import__("json").loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["sources"] == {"status": "healthy"}
    assert report["browser"] == {"status": "ready", "verified": True, "scope": "executor_transport"}
    assert report["oauth"] == {"status": "ready"}
    assert report["queue"] == {"leased_count": 0, "expired_lease_count": 0}
    assert report["tracker"] == {"drift_count": 0, "status": "in_sync"}
    assert report["notifications"] == {"status": "delivered", "verified": True}
