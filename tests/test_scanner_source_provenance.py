"""Synthetic saved-source rows; no employer data or live application authority."""
from test_scanner_observation import _report, JOB
import json
import sqlite3
import pytest
import scanner


@pytest.mark.parametrize('value', [False, None, 0, 1, 'false', 'true', '', [], {}])
def test_source_flag_requires_boolean_true_to_avoid_quarantine(tmp_path, value):
    payload = _report(tmp_path, [{**JOB, 'source_live_verified':value}])
    assert payload['new'] == 0
    assert payload['auto_apply_queue'] == []
    assert payload['observation_only_results'] == payload['all_results']


@pytest.mark.parametrize('flags', [
    {'source_live_verified':True, 'live_verified':False},
    {'source_live_verified':True, 'pilot_only':True},
    {'source_live_verified':False, 'live_verified':True, 'pilot_only':False},
    {'source_live_verified':True, 'live_verified':'true'},
    {'source_live_verified':True, 'pilot_only':0},
])
def test_conflicting_markers_cannot_promote_an_observation(tmp_path, flags):
    payload = _report(tmp_path, [{**JOB, **flags}])
    assert payload['new'] == 0
    assert payload['observation_only_results'] == payload['all_results']


@pytest.mark.parametrize('first_is_snapshot', [False, True])
def test_source_marker_survives_normalized_url_deduplication(tmp_path, first_is_snapshot):
    snapshot = {**JOB, 'url':JOB['url']+'?utm_source=synthetic', 'source_live_verified':False}
    jobs = [snapshot, JOB] if first_is_snapshot else [JOB, snapshot]
    payload = _report(tmp_path, jobs)
    assert payload['scanned'] == 1
    assert payload['new'] == 0
    assert payload['auto_apply_queue'] == []
    assert payload['observation_only_results'] == payload['all_results']
    assert payload['all_results'][0].get('source_live_verified') is (False if first_is_snapshot else None)


@pytest.mark.parametrize('flags', [{}, {'source_live_verified':True},
                                  {'source_live_verified':True, 'live_verified':True, 'pilot_only':False}])
def test_unmarked_or_explicit_true_retains_existing_discovery_hints(tmp_path, flags):
    payload = _report(tmp_path, [{**JOB, **flags}])
    assert payload['new'] == 1
    assert len(payload['auto_apply_queue']) == 1
    assert payload['observation_only_results'] == []


def test_source_snapshot_never_dispatches_a_manual_notification(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(scanner.subprocess, 'run', lambda *args, **kwargs: calls.append((args, kwargs)))
    payload = _report(tmp_path, [{**JOB, 'company':'Amazon', 'source_live_verified':False}], '--notify-maango')
    assert calls == []
    assert payload['manual_only'] == []
    assert payload['observation_only_results'][0]['manual_only'] is True


def test_source_snapshot_preserves_pending_duplicate_and_input_bytes(tmp_path):
    queue = tmp_path/'absent.sqlite3'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE application_queue (normalized_url TEXT, status TEXT)')
        db.execute('INSERT INTO application_queue VALUES (?, ?)', (JOB['url'], 'submission_pending'))
    before = queue.read_bytes()
    job = {**JOB, 'source_live_verified':False}
    payload = _report(tmp_path, [job])
    assert payload['all_results'][0]['duplicate'] is True
    assert payload['new'] == 0
    assert payload['observation_only_results'] == payload['all_results']
    assert queue.read_bytes() == before
    assert json.loads((tmp_path/'source.json').read_text(encoding="utf-8")) == [job]


def test_saved_source_boolean_false_stays_in_observation_report(tmp_path):
    job = {**JOB, 'source':'synthetic saved public snapshot', 'source_live_verified':False}
    payload = _report(tmp_path, [job])
    assert payload['new'] == 0
    assert payload['auto_apply_queue'] == []
    assert payload['manual_only'] == []
    assert payload['observation_only_results'] == payload['all_results']
    assert payload['all_results'][0]['source_live_verified'] is False
