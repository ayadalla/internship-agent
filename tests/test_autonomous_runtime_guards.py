"""Connected runtime regression guards; no employer or browser IO."""
import json
import pytest
from pathlib import Path
from test_autonomous_backend import backend_with_candidate, FakeWorker
from test_autonomous_controller import make_controller
from autonomous_controller import CandidateParked


def test_unknown_location_is_parked_even_without_definite_rejection(tmp_path):
    client=FakeWorker()
    backend,job=backend_with_candidate(tmp_path,client=client)
    catalog=json.loads(backend.catalog_path.read_text(encoding="utf-8"))
    catalog[job.url]['location']='Remote'
    backend.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(CandidateParked,match='eligibility_requires_verification'):
        backend._canonical_inputs(job)
    assert client.requests==[]


def test_pending_notification_does_not_block_next_hour_candidate(tmp_path):
    from datetime import timedelta
    controller,backend=make_controller(tmp_path,delivery_error=True)
    assert controller.run_once()['status']=='confirmed'
    later=controller.clock()+timedelta(hours=1,seconds=1)
    restarted,second=make_controller(tmp_path,delivery_error=True)
    restarted.clock=lambda:later
    assert restarted.run_once()['status']=='confirmed'
    assert second.calls.count('submit')==1
    assert [j.state for j in restarted.queue.list_jobs()]==['applied','applied']


def test_stop_during_review_prevents_submit_and_alternatives(tmp_path):
    controller,backend=make_controller(tmp_path)
    def stopped_review(*_):
        (controller.root/'STOP').touch()
    backend.review_authorize=stopped_review
    result=controller.run_once()
    assert 'submit' not in backend.calls
    assert result['status']=='stopped'
    assert all(j.state!='applied' for j in controller.queue.list_jobs())
