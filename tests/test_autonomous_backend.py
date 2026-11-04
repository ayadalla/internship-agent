"""Connected offline pipeline: fake browser/HTTP only, real operator stages."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class Response:
    def __init__(self, data): self.data=json.dumps(data).encode()
    def read(self): return self.data
    def __enter__(self): return self
    def __exit__(self,*_): pass


def config(tmp_path):
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'preferences':{'target_roles':['Software Engineer Intern']}}), encoding="utf-8")
    registry=tmp_path/'sources.json'
    registry.write_text(json.dumps({'version':1,'sources':[{'platform':'greenhouse','token':'schonfeld','approved':True},{'platform':'lever','token':'broken','approved':True}]}), encoding="utf-8")
    return {'runtime_dir':str(tmp_path/'runtime'), 'profile_path':str(profile), 'registry_path':str(registry),
            'worker_socket':str(tmp_path/'not-real.sock'), 'production_enabled':False, 'authorization_db':str(tmp_path/'ledger.db')}


def test_discovery_uses_existing_adapters_retains_exact_provenance_and_source_failure(tmp_path):
    from autonomous_backend import PipelineBackend
    import schonfeld_form
    calls=[]
    def opener(url, timeout):
        calls.append(url)
        if 'lever.co' in url: raise OSError('private network body')
        return Response({'jobs':[{'id':8171772,'title':'Software Engineer Intern','absolute_url':schonfeld_form.URL,
                                 'company_name':'Schonfeld','requisition_id':'P101884-2026-09-01','location':{'name':'New York, NY'}}]})
    backend=PipelineBackend(config(tmp_path), opener=opener)
    result=backend.discover()
    assert result['status']=='partial_error'
    assert len(result['jobs'])==1
    job=result['jobs'][0]
    assert job['identity']=={'company':'Schonfeld','role':'Software Engineer Intern','requisition':'P101884-2026-09-01','tenant':'schonfeld','platform':'greenhouse'}
    assert job['official_posting']['id']==8171772
    assert job['source']['token']=='schonfeld'
    assert job['source']['endpoint']==calls[0]
    failed=next(r for r in result['source_runs'] if r['status']=='error')
    assert failed['candidate_count'] is None
    assert 'private network' not in json.dumps(result)
    assert len(calls)==2


def accepted_profile(tmp_path):
    resume=tmp_path/'Approved Resume.pdf'
    resume.write_bytes(b'%PDF-1.7\nOFFLINE SYNTHETIC RESUME')
    return {'name':{'first':'Fixture'}, 'contact':{'email':'fixture@example.test','phone':'5550100'},
            'education':{'university':'University of California, San Diego','degree':'Bachelor of Science',
                         'major':'Data Science','gpa':3.236,'expected_graduation':'2028'},
            'work_authorization':True, 'requires_sponsorship':False,
            'screening_defaults':{'authorized_to_work_us':True,'require_sponsorship':False},
            'preferences':{'target_roles':['Technology Intern']},
            'resume':{'primary':str(resume),'required_application_filename':resume.name,'do_not_use_for_applications':[]},
            'application_facts':{'last_name':'Person','country':'United States','location':'New York, New York, United States',
                'education_start_month':'September','education_start_year':'2024',
                'education_end_month':'May','linkedin':'https://example.test/person',
                'graduation_window':'2028','gender':'Decline To Self Identify',
                'hispanic_latino':'Decline To Self Identify','race':'Decline To Self Identify'}}


def backend_with_candidate(tmp_path, **kwargs):
    from autonomous_backend import PipelineBackend
    from app_queue import ApplicationQueue
    from test_one_page_production import POSTING
    cfg=config(tmp_path)
    Path(cfg['profile_path']).write_text(json.dumps(accepted_profile(tmp_path)), encoding="utf-8")
    Path(cfg['registry_path']).write_text(json.dumps({'version':1,'sources':[{'platform':'greenhouse','token':'schonfeld','approved':True}]}), encoding="utf-8")
    # EXPLICIT SYNTHETIC qualification content; not sourced employer requirements.
    posting={**POSTING, 'content':'<!-- EXPLICIT SYNTHETIC TEST QUALIFICATIONS -->\n<h2>Qualifications</h2><p>Minimum GPA of 3.0</p><p>Graduation between 2027 and 2028</p>',
             'location':{'name':'New York, NY, United States'}}
    backend=PipelineBackend(cfg, opener=lambda *_:Response({'jobs':[posting]}), **kwargs)
    discovered=backend.discover()
    assert len(discovered['jobs'])==1
    candidate=discovered['jobs'][0]
    queue=ApplicationQueue(tmp_path/'queue.db')
    job=queue.enqueue(**{k:candidate[k] for k in ('company','role','url','ats_platform')})
    return backend, job


def test_automatic_manifest_and_answers_are_private_exact_and_profile_grounded(tmp_path):
    import stat
    import live_run_manifest
    from canonical_answers import verify_profile_answers
    backend, job=backend_with_candidate(tmp_path)
    original=backend.profile_path.read_bytes()
    from test_one_page_production import HTML
    attempt=backend.build_inputs(job, {'id':'new-target','url':job.url}, snapshot={'read_only':True, 'target_id':'new-target', 'url':job.url, 'html':HTML})
    manifest=live_run_manifest.load_manifest(attempt['manifest_path'])
    answers=json.loads(Path(attempt['answers_path']).read_text(encoding="utf-8"))
    verify_profile_answers(json.loads(original), answers, tenant='schonfeld')
    assert 'website' not in answers
    assert answers['gpa']=='3.236'
    assert answers['school']['exact_option']=='University of California - San Diego'
    assert manifest['job_id']==job.id
    assert manifest['identity']['requisition']=='P101884-2026-09-01'
    assert manifest['runtime_paths']['authorization_db']==backend.config['authorization_db']
    assert backend.profile_path.read_bytes()==original
    for key in ('manifest_path','answers_path','provenance_path'):
        assert stat.S_IMODE(Path(attempt[key]).stat().st_mode)==0o600
    provenance=json.loads(Path(attempt['provenance_path']).read_text(encoding="utf-8"))
    assert provenance['job_id']==job.id
    assert provenance['source']['token']=='schonfeld'


class FakeWorker:
    """Safe offline worker protocol, records fresh-target behavior."""
    def __init__(self): self.requests=[]; self.targets=[{'targetId':'user-tab','url':'https://example.test/work','type':'page'}]
    def health(self):
        self.requests.append({'action':'health'})
        return {'status':'ready','transport':'offline_fake'}
    def request(self, payload):
        self.requests.append(payload)
        if payload['action']=='list': return list(self.targets)
        if payload['action']=='new':
            assert payload.get('background') is True
            self.targets.append({'targetId':'new-target','url':payload['url'],'type':'page'})
            return {'targetId':'new-target'}
        raise AssertionError('Only health/list/new startup commands permitted')


def safe_page():
    import hashlib
    import worker_operator
    import schonfeld_form as form
    from datetime import datetime, timezone
    from test_one_page_production import HTML, IDENTITY, POSTING
    from test_client_bound_review import client_inputs
    class Page(worker_operator.WorkerPage):
        def __init__(self):
            super().__init__(target_id='new-target',target_url=form.URL,connection=None)
            self.values={}; self.upload=None; self.clicks=0; self.mutations=[]
        def __exit__(self,*_): pass
        def read_only_snapshot(self):
            if self.clicks:
                return self.read_after_submit_snapshot()
            return {'target_id':self.target_id,'url':form.URL,'html':HTML,'read_only':True,'official_posting':POSTING,'gates':[]}
        def inspect_safety_surface(self, selectors):
            return {'retina_scale':2.0,'control_visible':True,'overlay_present':False,'native_window_detected':False}
        def replace_text(self, selector, value): self.values[selector]=value; self.mutations.append(selector)
        def read_value(self, selector): return self.values.get(selector,'')
        def react_select_exact(self,selector,search_text,exact_option): self.replace_text(selector,exact_option)
        def read_react_selected_option(self, selector): return self.read_value(selector)
        def cdp_upload(self,selector,path): self.upload=Path(path); self.mutations.append(selector)
        def read_uploaded_filename(self,selector): return self.upload.name if self.upload else ''
        def read_greenhouse_client_bound_form(self):
            raw=client_inputs()['server_review']
            raw.update(target_id=self.target_id,page_url=form.URL,identity=IDENTITY,fields=dict(self.values),parser_repairs=[],
                       observed_at=datetime.now(timezone.utc).isoformat(),
                       questions=[{'id':k,'required':True,'answered':True,'verified':True,'selector':form.CONTROLS[k][0],
                                   'answer':self.values.get(form.CONTROLS[k][0])} for k in form.REQUIRED if k!='resume'])
            raw['bindings']={selector:{'count':1,'bound':True,'valid':True,'visible':True,'enabled':True,
                 'choice':[{'value':value,'label':value}]} for selector,value in self.values.items()}
            raw['completeness']['required_selectors']=[form.CONTROLS[k][0] for k in form.REQUIRED if k!='resume']
            raw['resume'].update(basename=self.upload.name, filename=self.upload.name,
                                 sha256=hashlib.sha256(self.upload.read_bytes()).hexdigest())
            self._reviewed_client_state=raw
            return raw
        def inspect_submit_control(self,selector):
            return {'visible':True,'enabled':True,'unique':True,'unobscured':True,'role':'button','selector':selector,'target_id':self.target_id,'url':form.URL}
        def _evaluate(self, expression):
            assert '/* schonfeld:submit */' in expression
            if '.click()' in expression: self.clicks+=1
            return {'visible':True,'enabled':True,'unique':True,'unobscured':True,'role':'button'}
        def read_after_submit_snapshot(self):
            assert self.clicks==1
            return {'target_id':self.target_id,'url':form.URL+'/thank_you','read_only':True,
                    'html':'<html><body><h1>Thank you for applying.</h1></body></html>','body_text':'Thank you for applying.'}
        def inspect_confirmation(self): return {'confirmed':False}
    return Page()


@pytest.mark.parametrize('notifications', [False,True])
def test_connected_pipeline_real_stages_generated_inputs_one_shot_confirmation(tmp_path, monkeypatch, notifications):
    from autonomous_controller import AutonomousController
    from submission_ledger import SubmissionLedger
    from app_queue import ApplicationQueue
    monkeypatch.setattr('builtins.input', lambda *_:pytest.fail('must never prompt'))
    page=safe_page(); client=FakeWorker(); modes=[]
    def transport_builder(client, **kwargs):
        modes.append(kwargs)
        class Transport:
            def bind_mutable_page_target(self, target_id):
                assert target_id=='new-target'
                return page
            bind_page_target=bind_mutable_page_target
        return Transport()
    backend, job=backend_with_candidate(tmp_path,client=client,transport_builder=transport_builder)
    backend.config['notifications_enabled']=notifications
    from production_operator import _TimedLocalDiscord
    backend.discord_adapter=_TimedLocalDiscord()
    ledger=SubmissionLedger(Path(backend.config['authorization_db']))
    controller=AutonomousController(tmp_path/'control',ApplicationQueue(tmp_path/'queue.db'),backend,ledger)
    result=controller.run_once()
    parked={p.name:p.read_text(encoding='utf-8') for p in (controller.root/'parked').glob('*.json')}
    assert result['status']=='confirmed', (result, [(j.id,j.state,j.last_error) for j in controller.queue.list_jobs()], parked)
    assert result['notification']['status']==('complete' if notifications else 'disabled')
    if notifications:
        assert result['notification']['tracker']['status']=='not_requested'
        assert result['notification']['discord']['readback_verified'] is True
    assert page.clicks==1
    assert page.mutations
    assert len([r for r in client.requests if r['action']=='new'])==1
    assert client.targets[0]['targetId']=='user-tab'
    assert any(m.get('allow_mutation') is True for m in modes)
    assert modes[-1].get('allow_mutation',False) is False
    assert controller.queue.list_jobs()[0].state=='applied'
    manifests=list(backend.root.glob('attempts/*/manifest.json'))
    assert len(manifests)==1
    manifest=json.loads(manifests[0].read_text(encoding="utf-8"))
    assert json.loads(Path(manifest['runtime_paths']['preparation']).read_text(encoding="utf-8"))['status']=='prepared'
    assert json.loads(Path(manifest['runtime_paths']['review']).read_text(encoding="utf-8"))['review']['review_authoritative'] is True
    assert json.loads(Path(manifest['runtime_paths']['confirmation']).read_text(encoding="utf-8"))['portal']['portal_confirmed'] is True
    assert controller.run_once()['status']=='hourly_limit'
    assert page.clicks==1


@pytest.mark.parametrize('case,reason', [('maango','maango_requires_separate_approval'),('unsupported','unsupported_form_family'),('missing_fact','required_canonical_fact_unavailable')])
def test_preflight_park_has_zero_browser_requests_or_manifest_mutation(tmp_path, case, reason):
    from dataclasses import replace
    from autonomous_controller import CandidateParked, atomic_json
    client=FakeWorker()
    backend,job=backend_with_candidate(tmp_path,client=client)
    catalog=json.loads(backend.catalog_path.read_text(encoding="utf-8"))
    if case=='maango':
        job=replace(job,company='Google')
        catalog[job.url]['company']='Google'
    elif case=='unsupported':
        value=catalog.pop(job.url)
        job=replace(job,url='https://job-boards.greenhouse.io/unknown/jobs/1')
        value['url']=job.url
        catalog[job.url]=value
    else:
        profile=json.loads(backend.profile_path.read_text(encoding="utf-8"))
        del profile['application_facts']['education_start_month']
        backend.profile_path.write_text(json.dumps(profile), encoding="utf-8")
    atomic_json(backend.catalog_path,catalog)
    with pytest.raises(CandidateParked) as error:
        backend.prepare(job,lambda **_:None)
    assert error.value.reason==reason
    assert client.requests==[]
    assert not list(backend.root.glob('attempts/*/manifest.json'))


def test_disabled_backend_cannot_contact_real_worker_even_if_called_directly(tmp_path):
    from autonomous_controller import CandidateParked
    backend,job=backend_with_candidate(tmp_path)
    with pytest.raises(CandidateParked, match='production_disabled'):
        backend.prepare(job,lambda **_:None)


def test_existing_matching_manifest_parks_job_without_worker_or_replay(tmp_path):
    from autonomous_controller import CandidateParked
    from test_one_page_production import IDENTITY
    client=FakeWorker()
    backend,job=backend_with_candidate(tmp_path,client=client)
    history=tmp_path/'history'; history.mkdir()
    (history/'manifest.json').write_text(json.dumps({'schema_version':1, 'identity':{**IDENTITY,'platform':'greenhouse','tenant':'schonfeld'},'target':{'id':'other-session','url':job.url}}), encoding="utf-8")
    backend.config['historical_artifact_roots']=[str(history)]
    with pytest.raises(CandidateParked, match='historical_application_requires_reconciliation'):
        backend.prepare(job,lambda **_:None)
    assert client.requests==[]


def test_unhealthy_existing_worker_does_not_create_a_tab(tmp_path):
    from autonomous_controller import CandidateParked
    client=FakeWorker(); client.health=lambda: {'status':'blocked'}
    backend,job=backend_with_candidate(tmp_path,client=client)
    with pytest.raises(CandidateParked, match='approved_worker_unavailable'):
        backend.prepare(job,lambda **_:None)
    assert client.requests==[]


def test_submit_missing_preparation_is_known_preintent_not_uncertain(tmp_path):
    from autonomous_controller import CandidateParked
    from test_one_page_production import HTML
    backend,job=backend_with_candidate(tmp_path,client=FakeWorker())
    attempt=backend.build_inputs(job,{'id':'new-target','url':job.url},snapshot={'read_only':True,'target_id':'new-target','url':job.url,'html':HTML})
    with pytest.raises(CandidateParked) as error:
        backend.submit(attempt,lambda **_:None)
    assert error.value.before_submit_intent is True
    assert error.value.reason=='submit_preflight_blocked'
