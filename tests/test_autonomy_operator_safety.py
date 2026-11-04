from pathlib import Path
import sys
import json
from datetime import datetime, timezone
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import production_operator as op


def test_production_uses_one_fixed_submission_store_across_manifest_paths(monkeypatch, tmp_path):
    factory = getattr(op, '_authorization_store', None)
    assert callable(factory), 'production authorization still uses a per-manifest database'
    calls = []
    monkeypatch.setattr(op.submission_authorization, 'SubmissionAuthorizationStore',
        lambda path, **kw: calls.append((str(path),kw)) or object())
    for name in ['first', 'second']:
        factory({'mode':'production_live','runtime_paths':{'authorization_db':str(tmp_path/name)}})
    assert calls[0] == calls[1]
    assert calls[0][1] == {'production': True}
    assert Path(calls[0][0]).as_posix().endswith('/Documents/job-agent/runtime/submission-ledger.sqlite3')


def test_live_prepare_enforces_recomputed_maango_before_browser_access(tmp_path):
    from test_production_operator_live import _write_live_inputs
    path, answers, manifest = _write_live_inputs(tmp_path)
    manifest['identity']['company'] = 'Amazon'
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match='MAANGO'):
        op.run_live_prepare(manifest_path=path, approved_answers_path=answers,
            step='application', cdp_base_url='http://127.0.0.1:9222', production_enabled=False,
            health_probe=lambda _: pytest.fail('browser must not be touched'))


def test_generic_prepare_rejects_answer_file_conflicting_with_canonical_profile(tmp_path):
    from test_production_operator_live import _write_live_inputs
    path, answers_path, manifest = _write_live_inputs(tmp_path)
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    answers['first_name'] = 'Wrong invented first name'
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    with pytest.raises(ValueError, match='canonical'):
        op.run_live_prepare(manifest_path=path, approved_answers_path=answers_path,
            step='application', cdp_base_url='http://127.0.0.1:9222', production_enabled=False,
            health_probe=lambda _: pytest.fail('wrong canonical answer reached browser'))


def test_review_expected_gpa_comes_from_profile_not_observed_answers():
    helper = getattr(op, '_independent_review_fields', None)
    assert callable(helper), 'Review must independently source expected facts'
    profile = {'education': {'gpa': '3.236'}}
    mapping = {'tenant': 'fixture', 'steps': {'application': {
        'controls': {'gpa': {'selector':'#gpa', 'operation':'replace_text'}},
        'required_fields': ['gpa']}}}
    observed = {'fields': {'#gpa': {'value':'9.999', 'verified':True}}}
    assert helper(profile, mapping, 'application', observed) == {'#gpa':'3.236'}


def test_review_phone_normalizes_punctuation_not_digits():
    profile={'contact':{'phone':'+1-555-0100'}}
    mapping={'tenant':'fixture','steps':{'application':{
        'controls':{'phone':{'selector':'#phone','operation':'replace_text'}},'required_fields':['phone']}}}
    raw={'fields':{'#phone':'+1 (555) 0100'}}
    expected=op._independent_review_fields(profile,mapping,'application',raw)
    assert raw['fields']['#phone'] == expected['#phone'] == '15550100'
    wrong={'fields':{'#phone':'+1 (555) 9999'}}
    expected=op._independent_review_fields(profile,mapping,'application',wrong)
    assert wrong['fields']['#phone'] != expected['#phone']


def test_delivery_default_never_constructs_tracker(tmp_path, monkeypatch):
    from test_production_operator_live import _write_live_inputs
    path, _, manifest = _write_live_inputs(tmp_path)
    portal = {
        'portal_confirmed':True, 'safe_for_post_submit':True, 'platform':'greenhouse',
        'identity':{k:manifest['identity'][k] for k in ('company','role','requisition')},
        'confirmation':{'url':manifest['target']['url'], 'reference_id':None, 'submitted':True, 'text_sha256':'a'*64},
        'portal_readback':{'matched_application_count':1,'state':'submitted','submitted':True,'verified':True},
        'human_required':[], 'evidence':{'sanitized':True,'two_source_reconciliation':True}}
    Path(manifest['runtime_paths']['confirmation']).write_text(json.dumps({
        'status':'portal_confirmed','job_identity':op._manifest_job_identity(manifest),'portal':portal}), encoding="utf-8")
    monkeypatch.setattr(op, '_TimedLocalTracker', lambda: pytest.fail('tracker default must be disabled'))
    out = op.run_live_delivery(manifest_path=path, submitted_date='2026-09-05', production_enabled=False,
        commit_external=False, discord_channel_id=None, discord_token_env='unused', tracker_adapter=None, discord_adapter=None)
    assert out['tracker']['status'] == 'not_requested'
    assert out['application_state'] == 'portal_confirmed'


def test_production_rejects_noncanonical_profile_even_when_manifest_hash_matches(tmp_path):
    from test_production_operator_live import _write_live_inputs
    _, _, manifest = _write_live_inputs(tmp_path)
    manifest['mode'] = 'production_live'
    with pytest.raises(ValueError, match='canonical profile'):
        op._verified_profile_resume(manifest)


def test_maango_metadata_cannot_hide_amazon_from_mutation_gate():
    verifier = getattr(op, '_verify_employer_approval', None)
    assert callable(verifier), 'production trusts caller-supplied MAANGO booleans'
    manifest = {'identity':{'company':'Amazon'},'target':{'url':'https://amazon.jobs/jobs/123'},
                'manual_gate':{'maango':False,'maango_approved':False,'gates':[]}}
    with pytest.raises(ValueError, match='MAANGO'):
        verifier(manifest)
