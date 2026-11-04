"""Connected official-source -> existing guarded production stages backend.

No browser/worker is launched. Source and page seams are injectable for offline
proof; production always uses the already approved private Unix WorkerClient.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import sources
from source_registry import load_registry
from scanner import rejection_reasons, maango_company
from autonomous_controller import atomic_json, CandidateParked
from app_queue import normalize_url


class PipelineBackend:
    def __init__(self, config: dict, *, opener=None, client=None, transport_builder=None, clock=None, discord_adapter=None,
                 captcha_transport_factory=None):
        self._captcha_transport_factory = captcha_transport_factory
        self.config = dict(config)
        self.root = Path(config['runtime_dir']).expanduser().resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.opener = opener or sources._default_open
        self.client, self.transport_builder = client, transport_builder
        self.discord_adapter = discord_adapter
        self.profile_path = Path(config['profile_path']).expanduser().resolve()
        self.catalog_path = self.root/'discovery-catalog.json'

    def discover(self):
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        registry = load_registry(self.config['registry_path'])
        jobs, runs = [], []
        for source in registry['sources']:
            platform, token = source['platform'], source['token']
            captured = {}
            def capture(url, timeout):
                with self.opener(url, timeout) as response:
                    content = response.read()
                captured.update(endpoint=url, payload=json.loads(content))
                return io.BytesIO(content)
            try:
                fetch = sources.fetch_greenhouse_jobs if platform == 'greenhouse' else sources.fetch_lever_jobs
                candidates = fetch(token, opener=capture)
                payload = captured['payload']
                raw_jobs = payload['jobs'] if platform == 'greenhouse' else payload
                by_url = {normalize_url(raw.get('absolute_url', raw.get('hostedUrl',''))):raw for raw in raw_jobs}
                for job in candidates:
                    raw = by_url[normalize_url(job['url'])]
                    requisition = raw.get('requisition_id') or raw.get('id')
                    if requisition is None or not str(requisition).strip():
                        raise ValueError('official posting identity missing')
                    job['company'] = str(raw.get('company_name') or job['company']).strip()
                    identity = {'company':job['company'], 'role':job['role'], 'requisition':str(requisition), 'platform':platform, 'tenant':token}
                    jobs.append({**job, 'ats_platform':platform, 'identity':identity,
                                 'source':{**source, 'endpoint':captured['endpoint'], 'fetched_at':self.clock().isoformat()},
                                 'official_posting':raw,
                                 'policy':{'rejection_reasons':rejection_reasons(job, profile),
                                           'maango':bool(maango_company(job['company'],job['url']))}})
                runs.append({**source, 'status':'ok', 'candidate_count':len(candidates)})
            except Exception:
                runs.append({**source, 'status':'error', 'candidate_count':None, 'reason':'source_fetch_or_schema_failed'})
        unique = {normalize_url(job['url']):job for job in jobs}
        atomic_json(self.catalog_path, unique)
        status = 'partial_error' if any(r['status']=='error' for r in runs) else 'ok'
        return {'status':status, 'jobs':list(unique.values()), 'source_runs':runs}

    def _candidate(self, job):
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        candidate = catalog.get(normalize_url(job.url))
        if not candidate or any(candidate[key] != getattr(job, key) for key in ('company','role','ats_platform')):
            raise CandidateParked('blocked_fact', 'official_discovery_identity_unavailable')
        return candidate

    def _canonical_inputs(self, job):
        import tenant_field_maps
        from canonical_answers import resolve_fact, CanonicalAnswerError, verify_profile_answers
        from resume_preflight import preflight_profile_resume
        candidate = self._candidate(job)
        profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        if maango_company(job.company, job.url):
            raise CandidateParked('blocked_approval', 'maango_requires_separate_approval')
        from scanner import classify
        classification = classify(candidate, profile, use_tracker=False)
        if classification['eligibility_status'] == 'needs_verification':
            raise CandidateParked('blocked_fact', 'eligibility_requires_verification')
        if classification.get('rejection_reasons') or rejection_reasons(candidate, profile):
            raise CandidateParked('failed', 'policy_ineligible')
        try:
            mapping = tenant_field_maps.resolve_field_map(page_url=job.url, platform=job.ats_platform)
        except tenant_field_maps.FieldMapError:
            raise CandidateParked('blocked_fact', 'unsupported_form_family') from None
        step = mapping.get('steps', {}).get('application', {})
        # Only an actually connected one-page family is enabled, not every map.
        if (mapping.get('tenant') != 'schonfeld' or not step or step.get('next_step') != 'review'
            or step.get('required_conditions')):
            raise CandidateParked('blocked_fact', 'unsupported_form_family')
        from posting_qualifications import qualification_decision
        decision = qualification_decision(candidate.get('official_posting'), profile)
        if decision['status'] != 'qualified':
            raise CandidateParked('blocked_fact', decision['reason'])
        candidate = {**candidate, 'qualification_decision':decision}
        answers = {}
        for field, control in step['controls'].items():
            if control['operation'] == 'submit':
                continue
            try:
                value = resolve_fact(profile, field, tenant=mapping['tenant'])
            except CanonicalAnswerError:
                if field in step['required_fields']:
                    raise CandidateParked('blocked_fact', 'required_canonical_fact_unavailable') from None
                continue
            if control['operation'] == 'react_select_exact':
                value = {'search_text':value, 'exact_option':value}
            answers[field] = value
        verify_profile_answers(profile, answers, tenant=mapping['tenant'])
        resume = preflight_profile_resume(self.profile_path)
        return candidate, mapping, answers, resume

    def build_inputs(self, job, target, *, snapshot):
        import hashlib
        import uuid
        import live_run_manifest
        from prepare_live_job import _dispatch_live_html
        candidate, mapping, answers, resume = self._canonical_inputs(job)
        if (snapshot.get('read_only') is not True or snapshot.get('target_id') != target['id']
            or snapshot.get('url') != job.url or target['url'] != job.url):
            raise CandidateParked('blocked_fact', 'exact_target_drift')
        if snapshot.get('gates'):
            raise CandidateParked('blocked_security', 'rendered_security_gate')
        identity = candidate['identity']
        _dispatch_live_html(html_text=snapshot['html'], page_url=job.url,
                            expected_identity={k:identity[k] for k in ('company','role','requisition')},
                            expected_platform=job.ats_platform, official_posting=candidate['official_posting'])
        attempt_dir = self.root/'attempts'/f'{job.id}-{uuid.uuid4().hex}'
        attempt_dir.mkdir(mode=0o700, parents=True)
        suffixes = {'preparation':'preparation.json','review':'review.json','authorization_handoff':'authorization.handoff',
                    'submit_journal':'submit.jsonl','confirmation':'confirmation.json','transaction_db':'transactions.sqlite3','status':'pipeline-status.json'}
        paths = {k:str(attempt_dir/v) for k,v in suffixes.items()}
        if self.config.get('production_enabled') is True:
            from submission_ledger import CANONICAL_LEDGER_PATH
            paths['authorization_db'] = str(CANONICAL_LEDGER_PATH)
        else:
            paths['authorization_db'] = str(Path(self.config['authorization_db']).resolve())
        manifest = {'schema_version':1, 'mode':'production_live' if self.config.get('production_enabled') is True else 'sanitized_local',
                    'job_id':job.id, 'queue_id':f'queue-{job.id}', 'target':dict(target), 'identity':identity,
                    'profile':{'path':str(self.profile_path), 'sha256':hashlib.sha256(self.profile_path.read_bytes()).hexdigest(),'verified':True},
                    'resume':{k:resume[k] for k in ('path','basename','content_type','sha256','verified')},
                    'manual_gate':{'gates':[], 'maango':False, 'maango_approved':False,'verified':True}, 'runtime_paths':paths}
        live_run_manifest.validate_manifest(manifest, production_enabled=self.config.get('production_enabled') is True)
        from posting_qualifications import content_hash
        attempt = {'job_id':job.id, 'qualification_binding':content_hash(candidate['qualification_decision']),
                   'manifest_path':str(attempt_dir/'manifest.json'), 'answers_path':str(attempt_dir/'answers.json'),
                   'provenance_path':str(attempt_dir/'provenance.json')}
        atomic_json(Path(attempt['manifest_path']), manifest)
        atomic_json(Path(attempt['answers_path']), answers)
        atomic_json(Path(attempt['provenance_path']), {**candidate,'job_id':job.id})
        return attempt

    def _worker(self):
        if self.client is None:
            if self.config.get('production_enabled') is not True:
                raise CandidateParked('blocked_fact', 'production_disabled')
            from worker_operator import WorkerClient
            self.client = WorkerClient(str(Path(self.config['worker_socket']).expanduser()))
        return self.client

    def _transport(self, target, *, official_posting=None, allow_mutation=False, resume_path=None, confirmation_context=None):
        from worker_operator import WorkerTransport
        builder = self.transport_builder or WorkerTransport
        kwargs = {'target':target, 'official_posting':official_posting, 'allow_mutation':allow_mutation}
        if allow_mutation:
            kwargs.update(preparation_step='application', approved_upload_path=resume_path)
        if confirmation_context is not None:
            kwargs['confirmation_context'] = confirmation_context
        return builder(self._worker(), **kwargs)

    def _historical_gate(self, candidate):
        roots = [self.root, *[Path(p).expanduser().resolve() for p in self.config.get('historical_artifact_roots', [])]]
        if self.config.get('production_enabled') is True:
            roots.append(Path.home()/'Documents/job-agent/runtime')
        for root in roots:
            for path in root.rglob('*manifest*.json'):
                try:
                    old = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise CandidateParked('blocked_fact', 'historical_artifact_unreadable') from None
                identity = old.get('identity', {})
                expected = candidate['identity']
                if (old.get('target', {}).get('url') == candidate['url'] or
                    all(identity.get(k) == expected[k] for k in ('platform', 'tenant', 'requisition'))):
                    raise CandidateParked('blocked_fact', 'historical_application_requires_reconciliation')

    def _try_captcha_on_new_target(self, job, target, snapshot):
        """Attempt one visible reCAPTCHA anchor in the exact new tab before preparation.

        Token-field presence is necessary but never sufficient: the worker and
        a second live frame inspection must find no other detectable gates.
        """
        from prepare_job import prepare_saved_html
        from prepare_live_job import _has_exact_identity, _validate_local_cdp_base_url
        from autonomous_controller import CandidateParked
        from scoped_cdp import ScopedCDPTransport
        from greenhouse_handler import _GreenhouseHTMLParser
        from security_gates import detect_security_gates
        import captcha_solver

        def security_kinds(page):
            parsed = _GreenhouseHTMLParser()
            parsed.feed(page['html'])
            visible_text = page.get('body_text')
            chunks = parsed.text_chunks + ([visible_text] if isinstance(visible_text, str) else [])
            kinds = {gate['type'] for gate in detect_security_gates(chunks)}
            lowered = ' '.join(chunks).casefold()
            if any(phrase in lowered for phrase in (
                'verify your email', 'verify email address', 'email verification', 'confirm your email address')):
                kinds.add('email_verification')
            if any(phrase in lowered for phrase in (
                'assessment', 'skills test', 'online evaluation', 'coding challenge')):
                kinds.add('assessment')
            return kinds

        if (snapshot.get('read_only') is not True or snapshot.get('target_id') != target['id']
            or snapshot.get('url') != target['url'] or not isinstance(snapshot.get('html'), str)):
            raise CandidateParked('blocked_fact', 'exact_target_snapshot_unavailable')
        inspection = prepare_saved_html(html_text=snapshot['html'], page_url=job.url)
        gates = inspection.get('manual_gates') or ([inspection['manual_gate']] if inspection.get('manual_gate') else [])
        reported = snapshot.get('gates')
        if reported is not None and reported != [] and (not isinstance(reported, list) or len(reported) != 1 or
                         not isinstance(reported[0], dict) or reported[0].get('type') != 'captcha'):
            raise CandidateParked('blocked_security', 'captcha_with_other_gate_or_identity_unverified')
        observed_security = security_kinds(snapshot)
        if observed_security - {'captcha'}:
            raise CandidateParked('blocked_security', 'captcha_with_other_gate_or_identity_unverified')
        if not gates and not reported and not captcha_solver.has_provider_frame(snapshot['html']):
            return snapshot
        if (any(not isinstance(gate, dict) or gate.get('type') != 'captcha' for gate in gates)
            or not all(_has_exact_identity(snapshot['html'], value) for value in (job.company, job.role))):
            raise CandidateParked('blocked_security', 'captcha_with_other_gate_or_identity_unverified')
        factory = self._captcha_transport_factory or ScopedCDPTransport
        try:
            cdp_base_url = self.config.get('captcha_cdp_base_url', 'http://127.0.0.1:18800')
            _validate_local_cdp_base_url(cdp_base_url)
            with factory(cdp_base_url).bind_page_target(target['id']) as bound:
                if bound.target_url != target['url']:
                    raise ValueError('exact CAPTCHA target URL drift')
                result = captcha_solver.attempt_solve(bound._connection, expected_url=target['url'])
        except (OSError, RuntimeError, ValueError, KeyError):
            raise CandidateParked('blocked_security', 'captcha_browser_transport_unavailable') from None
        if result.get('solved') is not True:
            if not gates and (result.get('detail') == 'no_visible_widget' or
                              (result.get('kind') == 'recaptcha_invisible' and
                               result.get('detail') == 'unsupported_challenge')):
                return snapshot
            raise CandidateParked('blocked_security', 'captcha_challenge_not_cleared')
        with self._transport(target, official_posting=inspection.get('official_posting')).bind_page_target(target['id']) as page:
            fresh = page.read_only_snapshot()
        if (fresh.get('read_only') is not True or fresh.get('target_id') != target['id']
            or fresh.get('url') != target['url'] or not isinstance(fresh.get('html'), str)):
            raise CandidateParked('blocked_security', 'captcha_target_drift_after_attempt')
        remaining = prepare_saved_html(html_text=fresh['html'], page_url=job.url)
        more = remaining.get('manual_gates') or ([remaining['manual_gate']] if remaining.get('manual_gate') else [])
        if fresh.get('gates') or more or security_kinds(fresh):
            raise CandidateParked('blocked_security', 'captcha_gate_still_present_after_attempt')
        try:
            with factory(cdp_base_url).bind_page_target(target['id']) as bound:
                if (bound.target_url != target['url'] or
                    captcha_solver._value(bound._connection, 'location.href') != target['url']):
                    raise ValueError('fresh CAPTCHA target drift')
                current = captcha_solver.detect(bound._connection)
        except (OSError, RuntimeError, ValueError, KeyError):
            raise CandidateParked('blocked_security', 'captcha_fresh_frame_inspection_unavailable') from None
        if current.get('kind') != 'recaptcha_v2' or current.get('cleared') is not True:
            raise CandidateParked('blocked_security', 'captcha_fresh_provider_state_unverified')
        return fresh

    def prepare(self, job, checkpoint):
        import production_operator as op
        candidate, mapping, answers, resume = self._canonical_inputs(job)
        self._historical_gate(candidate)
        worker = self._worker()
        if worker.health().get('status') != 'ready':
            raise CandidateParked('blocked_fact', 'approved_worker_unavailable')
        before = worker.request({'action':'list', 'match':['']})
        before_ids = {item['targetId'] for item in before}
        checkpoint(phase='opening_target')
        created = worker.request({'action':'new', 'url':job.url, 'background':True})
        target = {'id':created['targetId'], 'url':job.url}
        checkpoint(target=target, phase='preparing')
        after = worker.request({'action':'list', 'match':[job.url]})
        matches = [item for item in after if item.get('targetId') == target['id'] and item.get('url') == job.url and item.get('type') == 'page']
        if target['id'] in before_ids or len(matches) != 1:
            raise CandidateParked('blocked_fact', 'new_target_verification_failed')
        with self._transport(target, official_posting=candidate['official_posting']).bind_page_target(target['id']) as page:
            snapshot = page.read_only_snapshot()
        snapshot = self._try_captcha_on_new_target(job, target, snapshot)
        attempt = self.build_inputs(job, target, snapshot=snapshot)
        checkpoint(**attempt)
        kwargs = self._stage_kwargs(attempt, allow_mutation=True)
        prepared = op.run_live_prepare(**kwargs)
        if prepared.get('status') != 'prepared' or prepared.get('review_ready') is not True:
            raise CandidateParked('blocked_fact', 'complete_preparation_unavailable')
        return attempt

    def _manifest(self, attempt):
        from live_run_manifest import load_manifest
        return load_manifest(attempt['manifest_path'], production_enabled=self.config.get('production_enabled') is True)

    def _stage_kwargs(self, attempt, *, allow_mutation=False):
        manifest = self._manifest(attempt)
        posting = json.loads(Path(attempt['provenance_path']).read_text(encoding="utf-8"))['official_posting']
        return {'manifest_path':Path(attempt['manifest_path']), 'approved_answers_path':Path(attempt['answers_path']), 'step':'application',
                'cdp_base_url':'http://127.0.0.1:9222', 'production_enabled':self.config.get('production_enabled') is True,
                'health_probe':lambda _:self._worker().health(),
                'transport_factory':lambda _:self._transport(manifest['target'], official_posting=posting,
                                                             allow_mutation=allow_mutation, resume_path=manifest['resume']['path'])}

    def _recheck_qualifications(self, attempt):
        """Fresh approved official content must exactly match the frozen decision."""
        from posting_qualifications import qualification_decision, content_hash
        try:
            manifest = self._manifest(attempt)
            saved = json.loads(Path(attempt['provenance_path']).read_text(encoding="utf-8"))
            source = saved['source']
            if (source['platform'] != 'greenhouse' or source['token'] != manifest['identity']['tenant']
                or saved['identity'] != manifest['identity']
                or normalize_url(saved['url']) != normalize_url(manifest['target']['url'])
                or not any(s['platform'] == source['platform'] and s['token'] == source['token']
                           for s in load_registry(self.config['registry_path'])['sources'])):
                raise ValueError('official source binding drift')
            captured = {}
            def capture(url, timeout):
                with self.opener(url, timeout) as response:
                    raw = response.read()
                captured.update(json.loads(raw))
                return io.BytesIO(raw)
            sources.fetch_greenhouse_jobs(source['token'], opener=capture)
            postings = [p for p in captured['jobs']
                        if normalize_url(p['absolute_url']) == normalize_url(saved['url'])]
            if len(postings) != 1:
                raise ValueError('official posting missing or ambiguous')
            decision = qualification_decision(postings[0], json.loads(self.profile_path.read_text(encoding="utf-8")))
            if (decision['status'] != 'qualified' or decision != saved['qualification_decision']
                or content_hash(decision) != attempt['qualification_binding']
                or content_hash(saved['official_posting']) != decision['posting_sha256']):
                raise ValueError('posting qualification binding drift')
        except (OSError, ValueError, KeyError, TypeError):
            raise CandidateParked('blocked_fact', 'fresh_posting_qualification_unverified') from None

    def review_authorize(self, attempt, checkpoint):
        import production_operator as op
        checkpoint(phase='reviewing')
        wrapper, _ = op.run_live_review(**self._stage_kwargs(attempt), required_parser_repairs=[], required_question_ids=[])
        review = wrapper['review']
        if (wrapper.get('status') != 'reviewed' or review.get('review_authoritative') is not True
            or review.get('human_required') != [] or not review.get('review_evidence_sha256')):
            raise CandidateParked('blocked_fact', 'authoritative_complete_review_unavailable')
        self._recheck_qualifications(attempt)
        checkpoint(phase='authorizing')
        op.run_live_authorize(manifest_path=Path(attempt['manifest_path']), actor='autonomous-policy-v1',
                              approved_review_hash=review['review_evidence_sha256'], expires_in_seconds=300,
                              maango_approved=False, production_enabled=self.config.get('production_enabled') is True, clock=self.clock)

    def submit(self, attempt, checkpoint):
        import production_operator as op
        try:
            op.run_live_submit(**self._stage_kwargs(attempt), required_parser_repairs=[], required_question_ids=[],
                               actor='autonomous-policy-v1', maango_approved=False, clock=self.clock)
        except Exception:
            # Only this same-invocation failure may be classified pre-intent.
            # A restart always goes through reconcile(), never this branch.
            manifest = self._manifest(attempt)
            store = op._authorization_store(manifest)
            profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
            intent = store.ledger.attempt_for_application(job_id=manifest['job_id'],
                requisition=manifest['identity']['requisition'], **op._authorization_identity(manifest, profile))
            if intent is None and not Path(manifest['runtime_paths']['submit_journal']).exists():
                raise CandidateParked('blocked_fact', 'submit_preflight_blocked', before_submit_intent=True) from None
            raise
        return self.reconcile(attempt)

    def reconcile(self, attempt):
        import production_operator as op
        manifest = self._manifest(attempt)
        paths = manifest['runtime_paths']
        context = None
        transition = Path(paths['confirmation']+'.transition.json')
        if transition.exists():
            context = {'job_id':manifest['job_id'], 'journal_path':paths['submit_journal'],
                       'review_evidence':json.loads(Path(paths['review']).read_text(encoding="utf-8"))['review'],
                       'transition':json.loads(transition.read_text(encoding="utf-8"))}
        wrapper, _ = op.run_live_confirmation(manifest_path=Path(attempt['manifest_path']),
            cdp_base_url='http://127.0.0.1:9222', production_enabled=self.config.get('production_enabled') is True,
            health_probe=lambda _:self._worker().health(), clock=self.clock,
            transport_factory=lambda _:self._transport(manifest['target'], confirmation_context=context))
        return {'status':'confirmed' if wrapper.get('portal',{}).get('portal_confirmed') is True else 'uncertain'}

    def deliver(self, attempt):
        import production_operator as op
        production = self.config.get('production_enabled') is True
        if self.config.get('notifications_enabled', production) is not True:
            return {'status':'disabled'}
        manifest = self._manifest(attempt)
        date_path = Path(attempt['manifest_path']).parent/'delivery-date.json'
        if not date_path.exists():
            atomic_json(date_path, {'submitted_date':self.clock().date().isoformat()})
        submitted_date = json.loads(date_path.read_text(encoding="utf-8"))['submitted_date']
        return op.run_live_delivery(manifest_path=Path(attempt['manifest_path']), submitted_date=submitted_date,
            production_enabled=production, commit_external=production,
            discord_channel_id=self.config.get('discord_channel_id'),
            discord_token_env=self.config.get('discord_token_env','JOB_AGENT_DISCORD_BOT_TOKEN'),
            tracker_adapter=None, discord_adapter=self.discord_adapter, tracker_sync=False)
