import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from submission_ledger import SubmissionLedger


def test_cli_registers_history_before_any_discovery_or_browser(tmp_path, monkeypatch, capsys):
    import autonomous_controller as controller
    import submission_ledger
    from autonomous_backend import PipelineBackend
    home=tmp_path/'home';base=home/'Documents/job-agent';old=base/'runtime/old';old.mkdir(parents=True)
    profile=base/'profile.json';profile.write_text(json.dumps({'contact':{'email':'fixture@example.test'}}), encoding="utf-8")
    (old/'manifest.json').write_text(json.dumps({'mode':'production_live','profile':{'path':str(profile)},'runtime_paths':{'submit_journal':str(old/'missing.jsonl')}}), encoding="utf-8")
    config=tmp_path/'config.json';config.write_text(json.dumps({'production_enabled':True,'runtime_dir':str(base/'runtime/autonomous-controller'),'profile_path':str(profile),'registry_path':str(tmp_path/'never-read.json')}), encoding="utf-8")
    monkeypatch.setattr(Path,'home',lambda:home)
    monkeypatch.setattr(submission_ledger,'CANONICAL_LEDGER_PATH',base/'runtime/submission-ledger.sqlite3')
    monkeypatch.setattr(PipelineBackend,'discover',lambda *_: (_ for _ in ()).throw(AssertionError('discovery before migration')))
    assert controller.main(['run-once','--config',str(config)])==2
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='historical_evidence_blocked'


def test_legacy_intent_imported_idempotently_and_blocks_fresh_job(tmp_path):
    assert importlib.util.find_spec('legacy_submission_migration'), 'historical intent migration must exist'
    from legacy_submission_migration import migrate_registered_history
    old=tmp_path/'legacy'/'old-job';old.mkdir(parents=True)
    profile=tmp_path/'profile.json';profile.write_text('{}', encoding="utf-8")
    journal=old/'submit.jsonl'
    manifest={'mode':'production_live','job_id':3,
        'profile':{'path':str(profile)},
        'target':{'id':'target-old','url':'https://job-boards.greenhouse.io/example/jobs/123'},
        'identity':{'platform':'greenhouse','tenant':'example','company':'Example','role':'Intern','requisition':'123'},
        'runtime_paths':{'submit_journal':str(journal)}}
    (old/'manifest.json').write_text(json.dumps(manifest), encoding="utf-8")
    journal.write_text(json.dumps({'action':'submit','evidence':{'job_id':3,'target_id':'target-old',
        'page_url':manifest['target']['url'],'requisition':'123','status':'intent_recorded','verified':False}})+'\n', encoding="utf-8")
    ledger=SubmissionLedger(tmp_path/'ledger.sqlite3')
    kwargs={'runtime_root':tmp_path/'legacy','profile_path':profile,'account_id':'fixture@example.test','ledger':ledger}
    first=migrate_registered_history(**kwargs)
    second=migrate_registered_history(**kwargs)
    assert first['unresolved_count']==second['unresolved_count']==1
    assert ledger.status(now=datetime.now(timezone.utc))['eligible'] is False
    attempt=ledger.attempt_for_application(job_id=999,requisition='123',account_id='fixture@example.test',tenant='greenhouse:example')
    assert attempt['state']=='unknown'
    assert 'fixture@example.test' not in json.dumps(first)
