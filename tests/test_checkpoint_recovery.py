"""Expired-clock recovery uses real queue transactions, no replacement takeover."""
import json
from datetime import datetime, timezone, timedelta
import pytest
from test_autonomous_controller import make_controller
from app_queue import LeaseLostError

START=datetime(2026,9,5,12,tzinfo=timezone.utc)
EXPIRED=START+timedelta(seconds=301)


def crashed_controller(tmp_path, phase):
    first,backend=make_controller(tmp_path)
    first.clock=lambda:START
    discover=backend.discover
    backend.discover=lambda:{**discover(),'jobs':discover()['jobs'][:1]}
    if phase=='preparing':
        def crash(*_): raise SystemExit('offline preparation crash')
        backend.prepare=crash
    elif phase=='submitting':
        def crash(*_): raise SystemExit('offline post-intent crash')
        backend.submit=crash
    else:
        original=first._finish
        def crash(*args,**kwargs):
            if kwargs['outcome']=='applied': raise SystemExit('offline confirmed crash')
            return original(*args,**kwargs)
        first._finish=crash
    with pytest.raises(SystemExit): first.run_once()
    assert json.loads((first.root/'active-attempt.json').read_text(encoding="utf-8"))['phase']==phase
    return first


@pytest.mark.parametrize('phase', ['preparing','confirmed'])
def test_expired_checkpoint_finishes_without_resurrecting_old_capability(tmp_path,phase):
    first=crashed_controller(tmp_path,phase)
    old=first.queue.list_jobs()[0]
    restarted,backend=make_controller(tmp_path)
    restarted.clock=lambda:EXPIRED
    assert restarted.run_once()['status']=='confirmed'
    assert [j.state for j in restarted.queue.list_jobs()]==(['blocked_fact','applied'] if phase=='preparing' else ['applied'])
    assert backend.calls==(['discover','submit','deliver'] if phase=='preparing' else ['deliver'])
    with pytest.raises(LeaseLostError):
        restarted.queue.heartbeat(old.id,lease_token=old.lease_token,now=EXPIRED,lease_seconds=300)
    assert not (restarted.root/'active-attempt.json').exists()


@pytest.mark.parametrize('phase', ['preparing','confirmed'])
def test_reassigned_owner_is_not_overwritten_and_checkpoint_does_not_stall(tmp_path,phase):
    first=crashed_controller(tmp_path,phase)
    replacement=first.queue.lease_next(now=EXPIRED,lease_seconds=300)
    restarted,backend=make_controller(tmp_path)
    restarted.clock=lambda:EXPIRED
    result=restarted.run_once()
    assert result['status'] in {'confirmed','checkpoint_owner_conflict'}
    assert first.queue.validate_lease(replacement.id,lease_token=replacement.lease_token,now=EXPIRED)==replacement
    assert not (restarted.root/'active-attempt.json').exists()
    if phase=='confirmed':
        assert backend.calls==[]
        assert restarted.run_once()['status']=='confirmed'  # unrelated second job
    assert [j.state for j in restarted.queue.list_jobs()]==['leased','applied']
    assert list((restarted.root/'recovery-conflicts').glob('*.json'))


def test_expired_uncertain_intent_is_parked_but_observation_checkpoint_survives(tmp_path):
    first=crashed_controller(tmp_path,'submitting')
    restarted,backend=make_controller(tmp_path,submit_status='uncertain')
    restarted.clock=lambda:EXPIRED
    assert restarted.run_once()['status']=='submission_uncertain'
    assert restarted.queue.list_jobs()[0].state=='submission_uncertain'
    assert (restarted.root/'active-attempt.json').exists()
    assert restarted.run_once()['status']=='submission_uncertain'
    assert backend.calls==['reconcile','reconcile']


def test_atomic_checkpoint_recovery_rejects_live_replacement_expired_replacement_and_replay(tmp_path):
    from app_queue import ApplicationQueue
    queue=ApplicationQueue(tmp_path/'queue.db')
    job=queue.enqueue(company='Synthetic',role='Intern',url='https://example.test/1',ats_platform='test')
    old=queue.lease_next(now=START,lease_seconds=300)
    kwargs=dict(job_id=job.id,lease_token=old.lease_token,checkpoint_phase='preparing',outcome='blocked_fact')
    with pytest.raises(LeaseLostError): queue.recover_checkpoint(**kwargs,now=START)
    replacement=queue.lease_next(now=EXPIRED,lease_seconds=300)
    for now in (EXPIRED,EXPIRED+timedelta(seconds=301)):
        with pytest.raises(LeaseLostError): queue.recover_checkpoint(**kwargs,now=now)
        assert queue.list_jobs()[0]==replacement
    kwargs['lease_token']=replacement.lease_token
    now=EXPIRED+timedelta(seconds=301)
    with pytest.raises(ValueError):
        queue.recover_checkpoint(**{**kwargs,'checkpoint_phase':'submitting'},now=now)
    parked=queue.recover_checkpoint(**kwargs,now=now)
    assert parked.state=='blocked_fact' and parked.lease_token is None
    with pytest.raises(LeaseLostError): queue.recover_checkpoint(**kwargs,now=now)
    with pytest.raises(LeaseLostError): queue.finish_lease(job.id,lease_token=replacement.lease_token,outcome='applied',now=now)

