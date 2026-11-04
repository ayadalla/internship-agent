"""Direct authorization cannot evade registered legacy history (offline)."""
import json
from datetime import datetime, timezone
from pathlib import Path
import pytest


def test_direct_authorize_blocks_registered_history_without_review_or_handoff(tmp_path, monkeypatch):
    import production_operator as op
    import submission_ledger
    from test_one_page_production import one_page_inputs
    manifest_path, _, manifest, _ = one_page_inputs(tmp_path)
    home = tmp_path/'home'
    base = home/'Documents/job-agent'
    old = base/'runtime/old'
    old.mkdir(parents=True)
    profile = base/'profile.json'
    profile.write_text(json.dumps({'contact':{'email':'fixture@example.test'}}), encoding="utf-8")
    (old/'manifest.json').write_text(json.dumps({'mode':'production_live',
        'profile':{'path':str(profile)}, 'runtime_paths':{'submit_journal':str(old/'missing.jsonl')}}), encoding="utf-8")
    manifest['mode'] = 'production_live'
    manifest['profile']['path'] = str(profile)
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr(submission_ledger, 'CANONICAL_LEDGER_PATH', base/'runtime/submission-ledger.sqlite3')
    monkeypatch.setattr(op.live_run_manifest, 'load_manifest', lambda *a, **kw: manifest)
    monkeypatch.setattr(op, '_verified_profile_resume', lambda _: (json.loads(profile.read_text(encoding="utf-8")), {}))
    with pytest.raises(op.OperatorBlockedError, match='registered historical evidence'):
        op.run_live_authorize(manifest_path=manifest_path, actor='offline fixture',
            approved_review_hash='0'*64, expires_in_seconds=300, maango_approved=False,
            production_enabled=True, clock=lambda:datetime.now(timezone.utc))
    assert not Path(manifest['runtime_paths']['authorization_handoff']).exists()
