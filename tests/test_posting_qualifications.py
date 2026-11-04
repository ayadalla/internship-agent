"""SYNTHETIC posting constraints, never real employer qualification data."""
import json
from pathlib import Path
import pytest
from autonomous_controller import CandidateParked, atomic_json
from test_autonomous_backend import backend_with_candidate, FakeWorker


def change_body(backend, job, content):
    catalog=json.loads(backend.catalog_path.read_text(encoding="utf-8"))
    catalog[job.url]['official_posting']['content']=content
    # A caller's approval is deliberately not authority.
    catalog[job.url]['qualification_decision']={'status':'qualified'}
    atomic_json(backend.catalog_path, catalog)


@pytest.mark.parametrize('body', [
    'Qualifications\nMinimum GPA of 3.8',
    'Qualifications\nGraduation between 2026 and 2027',
    'Qualifications\nSecurity clearance required',
    'Qualifications\nProfessional license required',
    '',
    'Qualifications\nMust be currently enrolled in a bachelor’s degree program',
    'Qualifications\nU.S. citizenship required',
    'Qualifications\nRequired skills: Rust',
])
def test_unsupported_or_unmet_official_requirements_park_before_worker(tmp_path, body):
    client=FakeWorker()
    backend,job=backend_with_candidate(tmp_path,client=client)
    change_body(backend,job,body)
    with pytest.raises(CandidateParked) as error:
        backend.prepare(job,lambda **_:None)
    assert error.value.state=='blocked_fact'
    assert client.requests==[]
    assert not list(backend.root.glob('attempts/*/manifest.json'))


def test_matching_explicit_canonical_requirements_are_bound_not_answer_claims(tmp_path):
    backend,job=backend_with_candidate(tmp_path)
    profile=json.loads(backend.profile_path.read_text(encoding="utf-8"))
    profile['education']['currently_enrolled']=True
    profile['citizenship']='United States'
    profile['skills']=['Python','SQL']
    backend.profile_path.write_text(json.dumps(profile), encoding="utf-8")
    change_body(backend,job,'Qualifications\nMinimum GPA of 3.0\nGraduation between May 2027 and June 2028\n'
        "Must be currently enrolled in a bachelor's degree program\nU.S. citizenship required\nRequired skills: Python, SQL")
    candidate,_,_,_=backend._canonical_inputs(job)
    decision=candidate['qualification_decision']
    assert decision['status']=='qualified'
    assert len(decision['posting_sha256'])==64
    assert len(decision['profile_sha256'])==64
    assert len(decision['checks'])==5


@pytest.mark.parametrize('drift', ['official_body','missing_fresh_body','profile','forged_saved_decision','unavailable'])
def test_fresh_official_posting_drift_blocks_authorization(tmp_path, monkeypatch, drift):
    import production_operator as op
    from test_autonomous_backend import Response
    from test_one_page_production import HTML
    backend,job=backend_with_candidate(tmp_path,client=FakeWorker())
    attempt=backend.build_inputs(job,{'id':'new-target','url':job.url},snapshot={
        'read_only':True,'target_id':'new-target','url':job.url,'html':HTML})
    provenance=json.loads(Path(attempt['provenance_path']).read_text(encoding="utf-8"))
    if drift in {'official_body','missing_fresh_body'}:
        posting={**provenance['official_posting'], 'content':'Minimum GPA of 3.9' if drift=='official_body' else ''}
        backend.opener=lambda *_:Response({'jobs':[posting]})
    elif drift=='profile':
        profile=json.loads(backend.profile_path.read_text(encoding="utf-8"))
        profile['education']['gpa']=2.0
        backend.profile_path.write_text(json.dumps(profile), encoding="utf-8")
    elif drift=='forged_saved_decision':
        provenance['qualification_decision']={'status':'qualified'}
        Path(attempt['provenance_path']).write_text(json.dumps(provenance), encoding="utf-8")
    else:
        def unavailable(*_): raise OSError('offline simulated official source unavailable')
        backend.opener=unavailable
    monkeypatch.setattr(op,'run_live_review',lambda **_:({'status':'reviewed', 'review':{
        'review_authoritative':True,'human_required':[],'review_evidence_sha256':'a'*64}},None))
    authorized=[]
    monkeypatch.setattr(op,'run_live_authorize',lambda **_:authorized.append(True))
    with pytest.raises(CandidateParked) as error:
        backend.review_authorize(attempt,lambda **_:None)
    assert error.value.state=='blocked_fact'
    assert authorized==[]
