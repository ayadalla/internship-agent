"""Direct authorization with a disposable canonical home; no browser or live DB."""
import copy
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import production_operator as op
from symlink_support import symlink_or_skip
import submission_ledger
from posting_qualifications import content_hash
from test_production_operator_live import _write_live_inputs, _sha256
from test_submission_authorization import authoritative_review

NOW=datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


@pytest.fixture
def current_attempt(tmp_path, monkeypatch):
    path, _, manifest=_write_live_inputs(tmp_path)
    home=tmp_path/'home'
    base=home/'Documents/job-agent'
    current=base/'runtime/current-direct-attempt'
    current.parent.mkdir(parents=True)
    path.parent.rename(current)
    path=current/'manifest.json'
    profile=json.loads((current/'profile.json').read_text(encoding="utf-8"))
    profile['resume']['primary']=str(current/'Resume.pdf')
    (base/'profile.json').write_text(json.dumps(profile), encoding="utf-8")
    manifest['mode']='production_live'
    manifest['profile'].update(path=str(base/'profile.json'), sha256=_sha256(base/'profile.json'))
    manifest['resume']['path']=str(current/'Resume.pdf')
    manifest['runtime_paths']={key:str(current/Path(value).name)
                               for key,value in manifest['runtime_paths'].items()}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    review=authoritative_review()
    review['binding'].update(target_id=manifest['target']['id'], page_url=manifest['target']['url'],
                            **{k:manifest['identity'][k] for k in ('company','role','requisition')})
    review.pop('review_evidence_sha256')
    review['review_evidence_sha256']=content_hash(review)
    Path(manifest['runtime_paths']['review']).write_text(json.dumps({
        'status':'reviewed', 'job_identity':op._manifest_job_identity(manifest), 'review':review}), encoding="utf-8")
    monkeypatch.setattr(Path, 'home', lambda:home)
    monkeypatch.setattr(submission_ledger, 'CANONICAL_LEDGER_PATH', base/'runtime/submission-ledger.sqlite3')
    # Do not mock validation, preflight, migration, issuance, or the shared store.
    assert op.live_run_manifest.load_manifest(path, production_enabled=True)==manifest
    assert op._verified_profile_resume(manifest)[0]==profile
    return path, manifest, profile, review


def authorize(attempt):
    path, _, _, review=attempt
    return op.run_live_authorize(manifest_path=path, actor='offline fixture',
        approved_review_hash=review['review_evidence_sha256'], expires_in_seconds=300,
        maango_approved=False, production_enabled=True, clock=lambda:NOW)


def intent(manifest):
    return {'action':'submit', 'evidence':{'job_id':manifest['job_id'],
        'target_id':manifest['target']['id'], 'page_url':manifest['target']['url'],
        'requisition':manifest['identity']['requisition'], 'status':'intent_recorded', 'verified':False}}


def test_exact_validated_fresh_manifest_authorizes_without_creating_intent(current_attempt):
    _, manifest, _, review=current_attempt
    result=authorize(current_attempt)
    assert result['status']=='authorized'
    handoff=op._read_protected_authorization_handoff(Path(manifest['runtime_paths']['authorization_handoff']))
    assert handoff['review_evidence_sha256']==review['review_evidence_sha256']
    assert handoff['single_use'] is True
    assert not Path(manifest['runtime_paths']['submit_journal']).exists()
    store=op._authorization_store(manifest)
    with store.ledger.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM submission_authorizations WHERE used_at IS NULL').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM submission_attempts').fetchone()[0]==0


@pytest.mark.parametrize('history', ['missing', 'malformed_manifest', 'malformed_journal', 'intent', 'nested_missing', 'identical_copy'])
def test_other_history_is_never_exempted_by_current_manifest(current_attempt, history):
    path, manifest, _, _=current_attempt
    old_dir=path.parent/'nested-old' if history=='nested_missing' else path.parent.parent/'old'
    old_dir.mkdir()
    old=copy.deepcopy(manifest)
    old['runtime_paths']['submit_journal']=str(old_dir/'submit.jsonl')
    if history=='identical_copy':
        old=manifest  # Same content and absent journal, but NOT the validated path.
    (old_dir/'manifest.json').write_text('{' if history=='malformed_manifest' else json.dumps(old), encoding="utf-8")
    if history in {'intent','malformed_journal'}:
        Path(old['runtime_paths']['submit_journal']).write_text(json.dumps(intent(old)) if history=='intent' else '{', encoding="utf-8")
    with pytest.raises(op.OperatorBlockedError, match='registered historical evidence'):
        authorize(current_attempt)
    assert not Path(manifest['runtime_paths']['authorization_handoff']).exists()


@pytest.mark.parametrize('artifact', ['empty_journal', 'malformed_journal', 'intent', 'dangling_journal', 'handoff', 'dangling_handoff', 'old_authorization_db'])
def test_current_runtime_evidence_is_not_a_fresh_pre_intent_exception(current_attempt, artifact):
    _, manifest, _, _=current_attempt
    paths=manifest['runtime_paths']
    target=Path(paths['authorization_db'] if artifact=='old_authorization_db' else
                paths['authorization_handoff'] if 'handoff' in artifact else paths['submit_journal'])
    if artifact.startswith('dangling'):
        symlink_or_skip(target, target.parent/'unavailable')
    else:
        target.write_text(json.dumps(intent(manifest)) if artifact=='intent' else '' if artifact=='empty_journal' else '{', encoding="utf-8")
    before=target.read_bytes() if target.is_file() else None
    with pytest.raises((ValueError, PermissionError)):
        authorize(current_attempt)
    assert (target.read_bytes() if target.is_file() else None)==before
    store=op._authorization_store(manifest)
    with store.ledger.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM submission_authorizations').fetchone()[0]==0


@pytest.mark.parametrize('state', ['reserved', 'unknown', 'released', 'confirmed', 'legacy_consumed'])
def test_consumed_authorization_is_not_exempt_even_without_journal_or_handoff(current_attempt, state):
    _, manifest, profile, review=current_attempt
    store=op._authorization_store(manifest)
    identity=op._authorization_identity(manifest, profile)
    issued=store.issue(**identity, job_id=manifest['job_id'], review_evidence=review,
        actor='old fixture', issued_at=NOW, expires_at=NOW+timedelta(minutes=5))
    if state=='legacy_consumed':
        # Old tokens may predate account/tenant application keys and attempt IDs.
        with store.ledger.transaction() as connection:
            connection.execute('UPDATE submission_authorizations SET used_at=?, application_key=NULL', (NOW.isoformat(),))
        with pytest.raises((ValueError, PermissionError)):
            authorize(current_attempt)
        with store.ledger.transaction() as connection:
            assert connection.execute('SELECT count(*) FROM submission_authorizations').fetchone()[0]==1
            assert connection.execute("SELECT count(*) FROM submission_attempts WHERE state='unknown'").fetchone()[0]==1
        assert store.ledger.status(now=NOW)['reason']=='submission_pending'
        assert not Path(manifest['runtime_paths']['authorization_handoff']).exists()
        return
    consumed=store.consume(**identity, token=issued['token'], current_binding=issued['binding'], actor='old fixture', now=NOW)
    attempt_id=consumed['submission_attempt_id']
    if state=='unknown':
        store.ledger.mark_dispatch(attempt_id, now=NOW)
    elif state=='released':
        store.ledger.release_before_dispatch(attempt_id, now=NOW)
    elif state=='confirmed':
        store.ledger.mark_dispatch(attempt_id, now=NOW)
        store.ledger.confirm(attempt_id, now=NOW, confirmed=True)
    before=store.ledger.attempt_for_application(**identity, job_id=manifest['job_id'], requisition=manifest['identity']['requisition'])
    with pytest.raises((ValueError, PermissionError)):
        authorize(current_attempt)
    assert store.ledger.attempt_for_application(**identity, job_id=manifest['job_id'], requisition=manifest['identity']['requisition'])==before
    assert not Path(manifest['runtime_paths']['authorization_handoff']).exists()
    with store.ledger.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM submission_authorizations').fetchone()[0]==1


@pytest.mark.parametrize('drift', ['queue_id', 'mode', 'missing'])
def test_current_manifest_replaced_after_validation_is_not_exempt(current_attempt, monkeypatch, drift):
    path, manifest, _, _=current_attempt
    original=op._verified_profile_resume
    def replace_after_preflight(value):
        result=original(value)
        if drift=='missing':
            path.unlink(missing_ok=True)
        else:
            replacement={**manifest, drift:'sanitized_local' if drift=='mode' else 'different-validated-attempt'}
            path.write_text(json.dumps(replacement), encoding="utf-8")
        return result
    monkeypatch.setattr(op, '_verified_profile_resume', replace_after_preflight)
    with pytest.raises(op.OperatorBlockedError, match='pre-intent|registered historical evidence'):
        authorize(current_attempt)


def test_missing_current_journal_outside_attempt_scope_is_not_exempt(current_attempt):
    path, manifest, _, _=current_attempt
    manifest['runtime_paths']['submit_journal']=str(path.parent.parent/'outside.jsonl')
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(op.OperatorBlockedError, match='registered historical evidence'):
        authorize(current_attempt)
