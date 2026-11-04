"""Unattended policy orchestration; all ATS stages belong to the existing operator."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import file_lock
import platform_io


class CandidateParked(ValueError):
    """A known pre-intent blocker; safe to try an unrelated candidate."""
    def __init__(self, state: str, reason: str, *, before_submit_intent=False):
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.before_submit_intent = before_submit_intent


def atomic_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.checkpoint-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            platform_io.make_private(name, stream.fileno())
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        platform_io.fsync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class AutonomousController:
    def __init__(self, runtime_dir, queue, backend, ledger, *, clock=None,
                 max_candidates=5, lease_seconds=300, heartbeat_seconds=10):
        self.root = Path(runtime_dir).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.queue, self.backend, self.ledger = queue, backend, ledger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.max_candidates, self.lease_seconds = max_candidates, lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.state_path = self.root / 'controller-status.json'
        self._mutex = threading.RLock()
        self._leased = None
        self._heartbeat_failed = False
        self.stop_event = threading.Event()

    def status(self):
        return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {'status':'idle', 'notification':{'status':'disabled'}}

    def _status(self, status, **extra):
        with self._mutex:
            value = {**self.status(), 'status':status, 'heartbeat_at':self.clock().isoformat(), **extra}
            atomic_json(self.state_path, value)
            return value

    def _checkpoint(self, **changes):
        path = self.root / 'active-attempt.json'
        active = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        active.update(changes)
        atomic_json(path, active)
        return active

    def _confirmed_queue(self, active):
        job = next(j for j in self.queue.list_jobs() if j.id == active['job_id'])
        if job.state == 'applied':
            return
        if job.state == 'submission_uncertain':
            self.queue.transition(job.id, 'applied')
        else:
            self._finish(job.id, lease_token=active['lease_token'], outcome='applied', now=self.clock())

    def _deliver(self, attempt):
        active_path = self.root/'active-attempt.json'
        active = json.loads(active_path.read_text(encoding="utf-8"))
        from app_queue import LeaseLostError
        try:
            self._confirmed_queue(active)
        except LeaseLostError:
            self._archive_owner_conflict(active)
            return self._status('checkpoint_owner_conflict')
        pending = self.root/'notifications'/f"{int(active['job_id'])}.json"
        atomic_json(pending, {**active, **attempt})
        active_path.unlink(missing_ok=True)
        notification = self._retry_notification(pending, {**active, **attempt})
        return self._status('confirmed', notification=notification)

    def _retry_notification(self, path, attempt):
        try:
            notification = self.backend.deliver(attempt)
        except Exception:
            notification = {'status':'pending', 'reason':'delivery_failed'}
        if notification.get('status') in {'disabled', 'complete', 'delivered'}:
            path.unlink(missing_ok=True)
        return notification

    def _stopped(self):
        return self.stop_event.is_set() or (self.root/'STOP').exists()

    @contextmanager
    def _heartbeat(self):
        done = threading.Event()
        self._heartbeat_failed = False
        def tick():
            while not done.wait(self.heartbeat_seconds):
                try:
                    with self._mutex:
                        if self._leased is not None:
                            job = self._leased
                            self.queue.heartbeat(job.id, lease_token=job.lease_token, now=self.clock(), lease_seconds=self.lease_seconds)
                        self._status(self.status()['status'])
                except Exception:
                    self._heartbeat_failed = True
        thread = threading.Thread(target=tick, daemon=True, name='autonomous-heartbeat')
        thread.start()
        try:
            yield
        finally:
            done.set()
            thread.join()
            self._leased = None

    @contextmanager
    def _lock(self):
        lock_path = self.root/'controller.lock'
        platform_io.refuse_symlink(lock_path)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | platform_io.NOFOLLOW, 0o600)
        try:
            if not file_lock.try_lock(fd):
                yield False
                return
            yield True
        finally:
            os.close(fd)

    def run_once(self):
        with self._lock() as locked:
            if not locked:
                return {'status':'already_running'}
            if self._stopped():
                return self._status('stopped')
            with self._heartbeat():
                return self._run_locked()

    def service(self, *, poll_seconds=30, stop_event=None):
        if stop_event is not None:
            self.stop_event = stop_event
        with self._lock() as locked:
            if not locked:
                return {'status':'already_running'}
            with self._heartbeat():
                while not self._stopped():
                    try:
                        self._run_locked()
                    except Exception:
                        self._status('dependency_failure')
                    deadline = time.monotonic() + poll_seconds
                    while not self._stopped() and time.monotonic() < deadline:
                        self.stop_event.wait(min(self.heartbeat_seconds, max(0, deadline-time.monotonic())))
                return self._status('stopped')

    def _finish(self, job_id, *, lease_token, outcome, now, error=None):
        with self._mutex:
            self._leased = None
            from app_queue import LeaseLostError
            try:
                return self.queue.finish_lease(job_id, lease_token=lease_token, outcome=outcome, now=now, error=error)
            except LeaseLostError:
                active = json.loads((self.root/'active-attempt.json').read_text(encoding="utf-8"))
                if active.get('job_id') != job_id or active.get('lease_token') != lease_token:
                    raise
                return self.queue.recover_checkpoint(job_id, lease_token=lease_token,
                    checkpoint_phase=active.get('phase'), outcome=outcome, now=now, error=error)

    def _archive_owner_conflict(self, active):
        # Preserve the old checkpoint, never steal the replacement lease.
        import uuid
        atomic_json(self.root/'recovery-conflicts'/f"{active['job_id']}-{uuid.uuid4().hex}.json",
                    {**active, 'recovery':'queue_owner_changed'})
        (self.root/'active-attempt.json').unlink()

    def _park(self, active, state, reason):
        from app_queue import LeaseLostError
        try:
            self._finish(active['job_id'], lease_token=active['lease_token'], outcome=state, now=self.clock(), error=reason)
        except LeaseLostError:
            self._archive_owner_conflict(active)
            return
        atomic_json(self.root/'parked'/f"{active['job_id']}.json", {**active, 'phase':'parked', 'state':state, 'reason':reason})
        (self.root/'active-attempt.json').unlink()

    def _run_locked(self):
        for path in sorted((self.root/'notifications').glob('*.json')):
            notification = self._retry_notification(path, json.loads(path.read_text(encoding="utf-8")))
            self._status(self.status()['status'], notification=notification)
        active_path = self.root / 'active-attempt.json'
        if active_path.exists():
            active = json.loads(active_path.read_text(encoding="utf-8"))
            if active.get('phase') == 'confirmed':
                return self._deliver(active)
            if active.get('phase') in {'submitting', 'submission_uncertain'}:
                from app_queue import LeaseLostError
                job = next(j for j in self.queue.list_jobs() if j.id == active['job_id'])
                if job.state == 'leased':
                    try:
                        self._finish(job.id, lease_token=active['lease_token'], outcome='submission_uncertain', now=self.clock())
                    except LeaseLostError:
                        pass  # Keep observing the old intent; never overwrite its replacement owner.
                try:
                    result = self.backend.reconcile(active)
                except Exception:
                    return self._status('submission_uncertain', reconciliation='observation_unavailable')
                if result['status'] != 'confirmed':
                    return self._status('submission_uncertain')
                self._checkpoint(phase='confirmed')
                return self._deliver(active)
            self._park(active, 'blocked_fact', 'interrupted_pre_submit_requires_fresh_review')
        pacing = self.ledger.status(now=self.clock())
        if not pacing['eligible']:
            return self._status(pacing['reason'], pacing=pacing)
        discovered = self.backend.discover()
        self._status('discovering', source_runs=discovered.get('source_runs', []))
        if not discovered['jobs'] and (discovered.get('status') == 'partial_error' or any(r.get('status') == 'error' for r in discovered.get('source_runs', []))):
            return self._status('discovery_failed')
        for candidate in discovered['jobs']:
            self.queue.enqueue(**{key:candidate[key] for key in ('company','role','url','ats_platform')})
        for _ in range(self.max_candidates):
            if self._stopped():
                return self._status('stopped')
            pacing = self.ledger.status(now=self.clock())
            if not pacing['eligible']:
                return self._status(pacing['reason'])
            job = self.queue.lease_next(now=self.clock(), lease_seconds=self.lease_seconds)
            if job is None:
                return self._status('no_candidates')
            self._leased = job
            atomic_json(active_path, {'job_id':job.id, 'lease_token':job.lease_token, 'phase':'preparing'})
            checkpoint = self._checkpoint
            try:
                attempt = self.backend.prepare(job, checkpoint)
                checkpoint(**attempt)
                if self._stopped():
                    raise CandidateParked('blocked_approval', 'user_stop_before_submit')
                self.backend.review_authorize(attempt, checkpoint)
                if self._stopped():
                    raise CandidateParked('blocked_approval', 'user_stop_before_submit')
                if self._heartbeat_failed:
                    raise CandidateParked('blocked_fact', 'lease_heartbeat_failed')
                self.queue.heartbeat(job.id, lease_token=job.lease_token, now=self.clock(), lease_seconds=self.lease_seconds)
                checkpoint(phase='submitting')
                result = self.backend.submit(attempt, checkpoint)
                outcome = 'applied' if result['status'] == 'confirmed' else 'submission_uncertain'
                checkpoint(phase='confirmed' if outcome == 'applied' else outcome)
            except Exception as exc:
                active = json.loads(active_path.read_text(encoding="utf-8"))
                if active.get('phase') == 'submitting' and not (isinstance(exc, CandidateParked) and exc.before_submit_intent is True):
                    checkpoint(phase='submission_uncertain')
                    self._finish(job.id, lease_token=job.lease_token, outcome='submission_uncertain', now=self.clock())
                    return self._status('submission_uncertain')
                self._park(active, exc.state if isinstance(exc, CandidateParked) else 'blocked_fact',
                           exc.reason if isinstance(exc, CandidateParked) else 'pre_submit_stage_failed')
                if self._stopped():
                    return self._status('stopped')
                continue
            self._finish(job.id, lease_token=job.lease_token, outcome=outcome, now=self.clock())
            if outcome == 'applied':
                return self._deliver(attempt)
            return self._status('submission_uncertain')
        return self._status('candidate_limit')


def main(argv=None):
    """Foreground daemon, intended for a reviewed service manager, not cron."""
    import argparse
    import signal
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('run-once','status','service'))
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        root = Path(config['runtime_dir']).expanduser().resolve()
        if args.command == 'status':
            status_path = root/'controller-status.json'
            result = json.loads(status_path.read_text(encoding="utf-8")) if status_path.exists() else {'status':'not_started','notification':{'status':'disabled'}}
            print(json.dumps(result, sort_keys=True))
            return 0
        if config.get('production_enabled') is not True:
            print(json.dumps({'status':'disabled'}))
            return 2
        canonical_root = (Path.home()/'Documents/job-agent/runtime/autonomous-controller').resolve()
        if root != canonical_root:
            raise ValueError('one canonical controller runtime is required in production')
        from autonomous_backend import PipelineBackend
        from app_queue import ApplicationQueue
        from submission_ledger import CANONICAL_LEDGER_PATH, SubmissionLedger
        from legacy_submission_migration import migrate_registered_history
        profile_path = Path(config['profile_path']).expanduser().resolve()
        if profile_path != (Path.home()/'Documents/job-agent/profile.json').resolve():
            raise ValueError('canonical profile is required')
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        ledger = SubmissionLedger(CANONICAL_LEDGER_PATH, production=True)
        history = migrate_registered_history(runtime_root=canonical_root.parent,
            profile_path=profile_path, account_id=profile['contact']['email'], ledger=ledger)
        atomic_json(root/'historical-migration.json', history)
        if history['status']=='blocked':
            result={'status':'historical_evidence_blocked','unresolved_count':history['unresolved_count'],
                    'blocker_count':len(history['blockers']),'notification':{'status':'not_started'}}
            atomic_json(root/'controller-status.json', result)
            print(json.dumps(result,sort_keys=True))
            return 2
        backend = PipelineBackend(config)
        controller = AutonomousController(root, ApplicationQueue(root/'application-queue.sqlite3'), backend,
            ledger,
            max_candidates=int(config.get('max_candidates',5)), lease_seconds=int(config.get('lease_seconds',300)),
            heartbeat_seconds=float(config.get('heartbeat_seconds',10)))
        previous = {}
        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, lambda *_:controller.stop_event.set())
        try:
            result = controller.service(poll_seconds=float(config.get('poll_seconds',30))) if args.command == 'service' else controller.run_once()
        finally:
            for name, handler in previous.items(): signal.signal(name, handler)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in {'confirmed','hourly_limit','stopped','no_candidates','candidate_limit'} else 2
    except Exception:
        # No raw exceptions, profile facts, worker responses, tokens or credentials.
        print(json.dumps({'status':'configuration_or_dependency_failure'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
