"""Synthetic CLI replay: non-live pilot evidence must not become a queue candidate."""
import json
import sqlite3
import scanner


def test_nonlive_pilot_stays_observation_only_in_cli_report(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"preferences": {"target_roles": ["Software Engineer Intern"],
                                                  "target_levels": ["Intern"],
                                                  "location_preference": "United States"}}), encoding="utf-8")
    source = tmp_path / "input.json"
    job = {"company": "Example Research", "role": "Software Engineer Intern",
           "url": "https://example.com/jobs/1", "location": "United States",
           "live_verified": False, "pilot_only": True, "source": "synthetic saved evidence"}
    source.write_text(json.dumps([job]), encoding="utf-8")
    queue = tmp_path / "queue.sqlite3"
    with sqlite3.connect(queue) as db:
        db.execute("CREATE TABLE application_queue (normalized_url TEXT, status TEXT)")
        db.execute("INSERT INTO application_queue VALUES (?, ?)",
                   ("https://example.com/jobs/uncertain", "submission_pending"))
    before = {path: path.read_bytes() for path in (profile, source, queue)}
    output = tmp_path / "report.json"

    assert scanner.main([str(source), "--profile", str(profile), "--queue-db", str(queue),
                         "--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["new"] == 0
    assert payload["auto_apply_queue"] == []
    assert payload["manual_only"] == []
    assert payload["observation_only_results"] == payload["all_results"]
    assert payload["all_results"][0]["relevant"] is True  # Discovery fit, not authorization.
    assert payload["all_results"][0]["live_verified"] is False
    assert payload["all_results"][0]["url"] == job["url"]
    assert all(path.read_bytes() == data for path, data in before.items())


def _report(tmp_path, jobs, *extra):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(jobs), encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"preferences": {"target_roles": ["Software Engineer Intern"],
                                                  "target_levels": ["Intern"]}}), encoding="utf-8")
    output = tmp_path / "result.json"
    assert scanner.main([str(source), "--profile", str(profile), "--queue-db",
                         str(tmp_path / "absent.sqlite3"), "--output", str(output), *extra]) == 0
    return json.loads(output.read_text(encoding="utf-8"))


JOB = {"company": "Example Research", "role": "Software Engineer Intern",
       "location": "United States", "url": "https://example.com/jobs/1"}


def test_url_dedup_cannot_erase_a_later_observation_marker(tmp_path):
    later_snapshot = {**JOB, "url": JOB["url"] + "?utm_source=synthetic",
                      "live_verified": False}
    payload = _report(tmp_path, [JOB, later_snapshot])
    assert payload["new"] == 0
    assert payload["auto_apply_queue"] == []
    assert len(payload["observation_only_results"]) == 1
    assert payload["observation_only_results"][0]["url"] == JOB["url"]


def test_nonlive_manual_candidate_cannot_trigger_notification(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    payload = _report(tmp_path, [{**JOB, "company": "Amazon", "live_verified": False}],
                      "--notify-maango")
    assert calls == []
    assert payload["manual_only"] == []
    assert payload["observation_only_results"][0]["manual_only"] is True


def test_unmarked_candidate_retains_existing_discovery_behavior(tmp_path):
    payload = _report(tmp_path, [JOB])
    assert payload["new"] == 1
    assert len(payload["auto_apply_queue"]) == 1
    assert payload["observation_only_results"] == []


def test_non_boolean_provenance_is_not_treated_as_live(tmp_path):
    jobs = [{**JOB, "url": f'https://example.com/jobs/{i}', **flags}
            for i, flags in enumerate([{"live_verified": "true"}, {"live_verified": None},
                                       {"pilot_only": "false"}, {"pilot_only": 0},
                                       {"pilot_only": True}])]
    payload = _report(tmp_path, jobs)
    assert payload["new"] == 0
    assert len(payload["observation_only_results"]) == len(jobs)


def test_observation_of_existing_attempt_preserves_duplicate_state(tmp_path):
    queue = tmp_path / "absent.sqlite3"
    with sqlite3.connect(queue) as db:
        db.execute("CREATE TABLE application_queue (normalized_url TEXT, status TEXT)")
        db.execute("INSERT INTO application_queue VALUES (?, ?)", (JOB["url"], "submission_pending"))
    before = queue.read_bytes()
    payload = _report(tmp_path, [{**JOB, "pilot_only": True}])
    assert payload["all_results"][0]["duplicate"] is True
    assert payload["new"] == 0
    assert len(payload["observation_only_results"]) == 1
    assert queue.read_bytes() == before
