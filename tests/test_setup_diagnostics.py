from __future__ import annotations

import json
import runpy
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import setup_diagnostics


def write_profile(directory):
    resume_path = directory / "resume.pdf"
    resume_path.write_text("resume fixture", encoding="utf-8")
    profile_path = directory / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"email": "test@example.com"},
        "resume": {"primary": str(resume_path)},
    }), encoding="utf-8")
    return profile_path


def test_main_defaults_to_profile_next_to_script_with_isolated_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    write_profile(checkout)
    script = checkout / "setup_diagnostics.py"
    shutil.copyfile(setup_diagnostics.__file__, script)
    token = tmp_path / "token.json"
    token.write_text("token fixture", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)

    diagnostics = runpy.run_path(str(script))
    exit_code = diagnostics["main"](["--skip-browser", "--oauth-token", str(token)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["checks"]["profile"]["status"] == "ready"
    assert payload["checks"]["profile"]["resume_path"] == str(checkout / "resume.pdf")


def test_main_skips_optional_sheets_when_token_is_absent(tmp_path, monkeypatch, capsys):
    profile_path = write_profile(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    diagnostics = runpy.run_path(setup_diagnostics.__file__)

    exit_code = diagnostics["main"](["--profile", str(profile_path), "--skip-browser"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert payload["checks"]["oauth"]["status"] == "skipped"
    assert "--require-sheets" in payload["checks"]["oauth"]["message"]


def test_main_require_sheets_blocks_when_default_token_is_absent(tmp_path, monkeypatch, capsys):
    profile_path = write_profile(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    diagnostics = runpy.run_path(setup_diagnostics.__file__)

    exit_code = diagnostics["main"]([
        "--profile", str(profile_path), "--skip-browser", "--require-sheets",
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["checks"]["profile"]["status"] == "ready"
    assert payload["checks"]["oauth"]["status"] == "blocking"
    assert payload["checks"]["oauth"]["error_code"] == "missing_google_oauth_token"
    assert str(tmp_path / ".hermes/google_token.json") in payload["checks"]["oauth"]["message"]


def test_main_explicit_missing_oauth_token_still_blocks(tmp_path, capsys):
    profile_path = write_profile(tmp_path)

    exit_code = setup_diagnostics.main([
        "--profile", str(profile_path), "--skip-browser",
        "--oauth-token", str(tmp_path / "missing-token.json"),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["checks"]["oauth"]["error_code"] == "missing_google_oauth_token"


@pytest.mark.parametrize("browser_status, ready", [("ready", True), ("unreachable", False)])
def test_run_checks_still_probes_browser_when_sheets_is_optional(tmp_path, browser_status, ready):
    profile_path = write_profile(tmp_path)
    calls = []

    def probe_browser(base_url):
        calls.append(base_url)
        return {"status": browser_status}

    report = setup_diagnostics.run_checks(
        profile_path, probe_browser=probe_browser, browser_base_url="http://example.invalid:9222",
    )

    assert calls == ["http://example.invalid:9222"]
    assert report["ready"] is ready
    assert report["checks"]["oauth"]["status"] == "skipped"
    assert report["checks"]["browser"]["status"] == browser_status


@pytest.mark.parametrize("resume", [{}, {"primary": ""}, {"primary": "   "},
                                      {"primary": None}, {"primary": 123}])
def test_run_checks_blocks_missing_or_invalid_resume_path(tmp_path, resume):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"email": "test@example.com"}, "resume": resume,
    }), encoding="utf-8")

    report = setup_diagnostics.run_checks(profile_path, skip_browser=True)

    assert report["ready"] is False
    assert report["checks"]["profile"]["status"] == "blocking"
    assert report["checks"]["profile"]["error_code"] == "missing_resume_path"


def test_main_reports_nul_resume_path_as_blocking_json(tmp_path, capsys):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"email": "test@example.com"},
        "resume": {"primary": str(tmp_path / "resume\x00.pdf")},
    }), encoding="utf-8")

    exit_code = setup_diagnostics.main(["--profile", str(profile_path), "--skip-browser"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert exit_code == 1
    assert payload["status"] == "blocking"
    assert payload["ready"] is False
    assert payload["checks"]["profile"]["status"] == "blocking"
    assert payload["checks"]["profile"]["error_code"] == "unreadable_resume_file"


@pytest.mark.parametrize("resume_value", [".", "directory"])
def test_run_checks_blocks_directory_resume(tmp_path, monkeypatch, resume_value):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "directory").mkdir()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps({
        "contact": {"email": "test@example.com"}, "resume": {"primary": resume_value},
    }), encoding="utf-8")

    report = setup_diagnostics.run_checks(profile_path, skip_browser=True)

    assert report["ready"] is False
    assert report["checks"]["profile"]["status"] == "blocking"
    assert report["checks"]["profile"]["error_code"] == "missing_resume_file"


@pytest.mark.parametrize("profile_kind, error_code", [
    ("missing", "missing_profile"),
    ("malformed", "invalid_profile"),
    ("directory", "invalid_profile"),
    ("invalid_utf8", "invalid_profile"),
])
def test_main_reports_missing_or_unreadable_profile_as_json(tmp_path, capsys, profile_kind, error_code):
    profile_path = tmp_path / "profile.json"
    if profile_kind == "malformed":
        profile_path.write_text('{"resume":', encoding="utf-8")
    elif profile_kind == "directory":
        profile_path.mkdir()
    elif profile_kind == "invalid_utf8":
        profile_path.write_bytes(b"\xff")

    exit_code = setup_diagnostics.main(["--profile", str(profile_path), "--skip-browser"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert exit_code == 1
    assert payload["ready"] is False
    assert payload["checks"]["profile"]["status"] == "blocking"
    assert payload["checks"]["profile"]["error_code"] == error_code


@pytest.mark.parametrize("profile", [None, [], {"resume": None}, {"resume": []},
                                       {"contact": None}, {"contact": []}])
def test_run_checks_blocks_malformed_profile_structure_without_browser(tmp_path, profile):
    profile_path = write_profile(tmp_path)
    if isinstance(profile, dict):
        profile = {"resume": {"primary": str(tmp_path / "resume.pdf")}, **profile}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    report = setup_diagnostics.run_checks(
        profile_path, probe_browser=lambda url: pytest.fail("Browser must not be probed"),
    )

    assert report["ready"] is False
    assert report["checks"]["profile"]["error_code"] == "invalid_profile"
    assert report["checks"]["browser"]["error_code"] == "profile_blocking_issue"


@pytest.mark.parametrize('entrypoint', ['run_checks', 'cli'])
@pytest.mark.parametrize(('stat_error', 'error_code'), [
    (FileNotFoundError, 'missing_resume_file'),
    (PermissionError, 'unreadable_resume_file'),
])
def test_resume_stat_failure_returns_blocking_report_without_traceback(
    tmp_path, monkeypatch, capsys, entrypoint, stat_error, error_code
):
    profile_path = write_profile(tmp_path)
    resume_path = tmp_path / 'resume.pdf'
    real_stat, real_is_file = Path.stat, Path.is_file

    def failing_stat(path, *args, **kwargs):
        if path == resume_path:
            raise stat_error('injected resume metadata failure')
        return real_stat(path, *args, **kwargs)

    # Reproduce a successful old is_file() check followed by a failed stat().
    # The fixed implementation must also handle failure on its sole stat call.
    monkeypatch.setattr(Path, 'is_file', lambda path: True if path == resume_path else real_is_file(path))
    monkeypatch.setattr(Path, 'stat', failing_stat)
    monkeypatch.setenv('HOME', str(tmp_path))

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if entrypoint == 'cli':
        monkeypatch.setattr(sys, 'argv', [setup_diagnostics.__file__, '--profile', str(profile_path), '--skip-browser'])
        with pytest.raises(SystemExit) as exc:
            runpy.run_path(setup_diagnostics.__file__, run_name='__main__')
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert captured.err == ''
        report = json.loads(captured.out)
    else:
        report = setup_diagnostics.run_checks(
            profile_path, probe_browser=lambda url: pytest.fail('Browser must not be probed'),
        )
        assert report['checks']['browser']['error_code'] == 'profile_blocking_issue'

    assert report['status'] == 'blocking'
    assert report['ready'] is False
    assert report['checks']['profile']['status'] == 'blocking'
    assert report['checks']['profile']['error_code'] == error_code
    assert str(resume_path) in report['checks']['profile']['message']


def test_resume_type_and_size_use_one_stat_snapshot(tmp_path, monkeypatch):
    profile_path = write_profile(tmp_path)
    resume_path = tmp_path / 'resume.pdf'
    real_stat = Path.stat
    resume_stat_calls = []

    def tracking_stat(path, *args, **kwargs):
        if path == resume_path:
            resume_stat_calls.append(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', tracking_stat)
    report = setup_diagnostics.run_checks(profile_path, skip_browser=True)

    assert report['ready'] is True
    assert resume_stat_calls == [resume_path]


def test_run_checks_blocks_empty_resume_file(tmp_path):
    profile_path = write_profile(tmp_path)
    (tmp_path / "resume.pdf").write_bytes(b"")

    report = setup_diagnostics.run_checks(profile_path, skip_browser=True)

    assert report["ready"] is False
    assert report["checks"]["profile"]["status"] == "blocking"
    assert report["checks"]["profile"]["error_code"] == "empty_resume_file"


@pytest.mark.parametrize("email", [None, "", "   ", 42, ["test@example.com"], {"value": "test@example.com"}])
def test_run_checks_blocks_invalid_contact_email(tmp_path, email):
    profile_path = write_profile(tmp_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["contact"]["email"] = email
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    report = setup_diagnostics.run_checks(profile_path, skip_browser=True)

    assert report["ready"] is False
    assert report["checks"]["profile"]["error_code"] == "missing_contact_email"


def test_run_checks_reports_blocking_profile_issues_without_touching_browser(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "contact": {"email": "test@example.com"},
                "resume": {"primary": str(tmp_path / "missing-resume.pdf")},
            }
        )
    , encoding="utf-8")

    calls: list[str] = []

    def fake_probe_browser(base_url: str) -> dict:
        calls.append(base_url)
        return {"status": "ready", "recoverable": False, "error_code": None}

    report = setup_diagnostics.run_checks(
        profile_path,
        browser_base_url="http://127.0.0.1:9222",
        probe_browser=fake_probe_browser,
        oauth_token_path=tmp_path / "missing-token.json",
    )

    assert report["status"] == "blocking"
    assert report["ready"] is False
    assert report["checks"]["profile"]["status"] == "blocking"
    assert report["checks"]["profile"]["error_code"] == "missing_resume_file"
    assert report["checks"]["browser"]["status"] == "skipped"
    assert calls == []


def test_main_can_skip_browser_and_still_report_ready_for_offline_setup(tmp_path, capsys):
    profile_path = tmp_path / "profile.json"
    resume_path = tmp_path / "resume.pdf"
    oauth_token = tmp_path / "google_token.json"
    resume_path.write_text("resume fixture", encoding="utf-8")
    oauth_token.write_text("token fixture", encoding="utf-8")
    profile_path.write_text(
        json.dumps(
            {
                "contact": {"email": "test@example.com"},
                "resume": {"primary": str(resume_path)},
            }
        )
    , encoding="utf-8")

    exit_code = setup_diagnostics.main(
        [
            "--profile",
            str(profile_path),
            "--oauth-token",
            str(oauth_token),
            "--skip-browser",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ready"
    assert payload["checks"]["oauth"]["status"] == "ready"
    assert "API access not verified" in payload["checks"]["oauth"]["message"]
    assert payload["checks"]["browser"]["status"] == "skipped"
    assert payload["checks"]["browser"]["error_code"] == "skipped_by_flag"
