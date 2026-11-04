"""Offline controller policy tests: real queue, controlled pipeline boundary."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_parked_first_candidate_continues_to_success_without_user_input(tmp_path, monkeypatch):
    from autonomous_controller import AutonomousController, CandidateParked
    from app_queue import ApplicationQueue
    monkeypatch.setattr('builtins.input', lambda *_: (_ for _ in ()).throw(AssertionError('no questions')))
    calls = []
    class Backend:
        def discover(self):
            return {'jobs': [dict(company='Example', role='SWE Intern', url=f'https://jobs.lever.co/example/{n}', ats_platform='lever') for n in (1,2)], 'source_runs': [{'status':'ok'}]}
        def prepare(self, job, checkpoint):
            calls.append(('prepare', job.id))
            if job.id == 1:
                raise CandidateParked('blocked_security', 'captcha')
            return {'job_id':job.id}
        def review_authorize(self, attempt, checkpoint): calls.append(('review',attempt['job_id']))
        def submit(self, attempt, checkpoint):
            calls.append(('submit',attempt['job_id']))
            return {'status':'confirmed'}
        def deliver(self, attempt): return {'status':'disabled'}
    class Ledger:
        def status(self, *, now): return {'eligible':True, 'reason':'eligible'}
    queue = ApplicationQueue(tmp_path/'queue.db')
    controller = AutonomousController(tmp_path/'control', queue, Backend(), Ledger(), clock=lambda: datetime.now(timezone.utc))
    result = controller.run_once()
    assert result['status'] == 'confirmed'
    assert [(j.id,j.state) for j in queue.list_jobs()] == [(1,'blocked_security'),(2,'applied')]
    assert calls == [('prepare',1),('prepare',2),('review',2),('submit',2)]
    assert controller.status()['notification']['status'] == 'disabled'


def make_controller(tmp_path, *, submit_status='confirmed', delivery_error=False):
    from autonomous_controller import AutonomousController
    from app_queue import ApplicationQueue
    class Backend:
        calls = []
        def discover(self):
            self.calls.append('discover')
            return {'jobs':[dict(company='Example',role='SWE Intern',url=f'https://jobs.lever.co/example/{n}',ats_platform='lever') for n in (1,2)], 'source_runs':[]}
        def prepare(self, job, checkpoint):
            checkpoint(manifest_path=str(tmp_path / f'{job.id}.json'))
            return {'job_id':job.id}
        def review_authorize(self, attempt, checkpoint): pass
        def submit(self, attempt, checkpoint):
            self.calls.append('submit')
            return {'status':submit_status}
        def reconcile(self, attempt):
            self.calls.append('reconcile')
            return {'status':submit_status}
        def deliver(self, attempt):
            self.calls.append('deliver')
            if delivery_error: raise RuntimeError('private upstream payload must not leak')
            return {'status':'disabled'}
    class Ledger:
        def status(self, *, now): return {'eligible':True,'reason':'eligible'}
    backend = Backend()
    return AutonomousController(tmp_path/'control', ApplicationQueue(tmp_path/'queue.db'), backend, Ledger()), backend


def test_uncertain_submit_stops_alternatives_and_restart_reconciles_only(tmp_path):
    first, backend = make_controller(tmp_path, submit_status='uncertain')
    assert first.run_once()['status'] == 'submission_uncertain'
    second, restarted = make_controller(tmp_path, submit_status='uncertain')
    assert second.run_once()['status'] == 'submission_uncertain'
    assert restarted.calls == ['reconcile']
    assert [(j.id,j.state) for j in first.queue.list_jobs()] == [(1,'submission_uncertain'),(2,'discovered')]


def test_confirmed_delivery_failure_restart_never_resubmits(tmp_path):
    controller, backend = make_controller(tmp_path, delivery_error=True)
    result = controller.run_once()
    assert result['status'] == 'confirmed'
    assert result['notification']['status'] == 'pending'
    restarted, calls = make_controller(tmp_path, delivery_error=True)
    restarted.ledger.status=lambda **_: {'eligible':False,'reason':'hourly_limit'}
    assert restarted.run_once()['status'] == 'hourly_limit'
    assert calls.calls == ['deliver']
    assert controller.queue.list_jobs()[0].state == 'applied'
    assert 'private upstream' not in json.dumps(restarted.status())


def test_parallel_run_excluded_before_discovery(tmp_path):
    import file_lock
    controller, backend = make_controller(tmp_path)
    with (controller.root/'controller.lock').open('w') as held:
        assert file_lock.try_lock(held.fileno())
        assert controller.run_once()['status'] == 'already_running'
    assert backend.calls == []


def test_exception_after_submit_boundary_is_durable_and_blocks_alternatives(tmp_path):
    controller, backend = make_controller(tmp_path)
    def disconnected(*_): raise RuntimeError('secret browser response')
    backend.submit = disconnected
    assert controller.run_once()['status'] == 'submission_uncertain'
    assert controller.queue.list_jobs()[1].state == 'discovered'
    active = json.loads((controller.root/'active-attempt.json').read_text(encoding="utf-8"))
    assert active['phase'] == 'submission_uncertain'
    assert 'secret browser' not in json.dumps(controller.status())


def test_restart_before_submit_preserves_tab_and_parks_instead_of_repreparing(tmp_path):
    import pytest
    controller, backend = make_controller(tmp_path)
    def crash(*_): raise SystemExit('simulated process death')
    backend.review_authorize = crash
    with pytest.raises(SystemExit): controller.run_once()
    restarted, backend2 = make_controller(tmp_path)
    restarted.max_candidates = 0
    restarted.run_once()
    assert restarted.queue.list_jobs()[0].state == 'blocked_fact'
    assert 'submit' not in backend2.calls
    assert list((restarted.root/'parked').glob('*.json'))


def test_active_lease_heartbeat_continues_during_stage_and_stop_does_not_kill_stage(tmp_path):
    import threading
    import time
    controller, backend=make_controller(tmp_path)
    controller.heartbeat_seconds=0.01
    seen=[]
    original=controller.queue.heartbeat
    def renew(*args,**kwargs):
        seen.append(kwargs['lease_token'])
        return original(*args,**kwargs)
    controller.queue.heartbeat=renew
    original_submit=backend.submit
    def slow_submit(*args):
        (controller.root/'STOP').touch()
        time.sleep(0.045)
        return original_submit(*args)
    backend.submit=slow_submit
    assert controller.run_once()['status']=='confirmed'
    assert len(seen)>=2
    assert controller.run_once()['status']=='stopped'
    assert backend.calls.count('submit')==1


def test_service_continuously_runs_without_catchup_then_exits_stop(tmp_path):
    controller,backend=make_controller(tmp_path)
    count=[]
    def tick():
        count.append(1)
        if len(count)==2: (controller.root/'STOP').touch()
        return controller._status('idle')
    controller._run_locked=tick
    result=controller.service(poll_seconds=0.001)
    assert result['status']=='stopped'
    assert len(count)==2


def test_ledger_pacing_checked_before_source_io_no_catchup(tmp_path):
    controller,backend=make_controller(tmp_path)
    controller.ledger.status=lambda **_: {'eligible':False,'reason':'hourly_limit','next_eligible_at':'2026-09-06T00:00:00+00:00'}
    assert controller.run_once()['status']=='hourly_limit'
    assert backend.calls==[]


def test_status_cli_is_readonly_and_disabled_cli_does_not_open_runtime(tmp_path,capsys):
    from autonomous_controller import main
    cfg=tmp_path/'config.json'
    runtime=tmp_path/'absent'
    cfg.write_text(json.dumps({'production_enabled':False,'runtime_dir':str(runtime)}), encoding="utf-8")
    assert main(['status','--config',str(cfg)])==0
    assert json.loads(capsys.readouterr().out)['status']=='not_started'
    assert not runtime.exists()
    assert main(['run-once','--config',str(cfg)])==2
    assert json.loads(capsys.readouterr().out)['status']=='disabled'
    assert not runtime.exists()


def test_discovery_failure_is_not_reported_as_zero_candidates(tmp_path):
    controller,backend=make_controller(tmp_path)
    backend.discover=lambda: {'jobs':[],'status':'partial_error','source_runs':[{'status':'error','candidate_count':None}]}
    assert controller.run_once()['status']=='discovery_failed'


def test_heartbeat_loss_blocks_submit(tmp_path):
    controller,backend=make_controller(tmp_path)
    original=backend.review_authorize
    def lost(*args):
        original(*args)
        controller._heartbeat_failed=True
    backend.review_authorize=lost
    controller.run_once()
    assert 'submit' not in backend.calls


def test_recovery_positive_confirmation_updates_queue_without_resubmit(tmp_path):
    first,_=make_controller(tmp_path,submit_status='uncertain')
    first.run_once()
    restarted,backend=make_controller(tmp_path,submit_status='confirmed')
    assert restarted.run_once()['status']=='confirmed'
    assert backend.calls==['reconcile','deliver']
    assert restarted.queue.list_jobs()[0].state=='applied'


def test_uncertain_confirmation_reader_failure_remains_uncertain(tmp_path):
    first,_=make_controller(tmp_path,submit_status='uncertain')
    first.run_once()
    restarted,backend=make_controller(tmp_path)
    def fails(*_): raise RuntimeError('raw portal failure')
    backend.reconcile=fails
    assert restarted.run_once()['status']=='submission_uncertain'
    assert 'submit' not in backend.calls


def test_crash_after_confirmed_checkpoint_before_queue_finish_recovers(tmp_path):
    import pytest
    first,backend=make_controller(tmp_path)
    original=first._finish
    def crash(*args,**kwargs):
        if kwargs['outcome']=='applied': raise SystemExit('crash before queue finish')
        return original(*args,**kwargs)
    first._finish=crash
    with pytest.raises(SystemExit): first.run_once()
    restarted,backend2=make_controller(tmp_path)
    assert restarted.run_once()['status']=='confirmed'
    assert restarted.queue.list_jobs()[0].state=='applied'
    assert backend2.calls==['deliver']


def test_known_failure_before_any_submit_intent_can_continue_alternative(tmp_path):
    from autonomous_controller import CandidateParked
    controller,backend=make_controller(tmp_path)
    original=backend.submit
    def submit(attempt, checkpoint):
        if attempt['job_id']==1:
            raise CandidateParked('blocked_fact','submit_preflight_blocked', before_submit_intent=True)
        return original(attempt,checkpoint)
    backend.submit=submit
    assert controller.run_once()['status']=='confirmed'
    assert [j.state for j in controller.queue.list_jobs()]==['blocked_fact','applied']
