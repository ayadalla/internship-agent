"""Exact one-page production path with offline dependencies only."""
import copy
import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_live_job
import schonfeld_form

URL = schonfeld_form.URL
IDENTITY = {"company": "Schonfeld", "role": "2027 DMFI Technology Intern", "requisition": "P101884-2026-09-01"}
HTML = '<html><h1>2027 DMFI Technology Intern</h1><p>Schonfeld</p><form><input id="first_name" required><button type="submit">Submit</button></form></html>'
POSTING = {"id":8171772,"absolute_url":URL,"title":IDENTITY["role"],"requisition_id":IDENTITY["requisition"],"company_name":"Schonfeld "}


def test_official_posting_binds_nonvisible_requisition_without_faking_html():
    try:
        result = prepare_live_job._dispatch_live_html(html_text=HTML, page_url=URL,
            expected_identity=IDENTITY, expected_platform="greenhouse", official_posting=POSTING)
    except TypeError:
        pytest.fail("official posting provenance input missing")
    assert result["requisition"] == IDENTITY["requisition"]
    assert result["identity_source"] == "official_greenhouse_posting"
    assert IDENTITY["requisition"] not in HTML


def one_page_inputs(tmp_path):
    from test_production_operator_live import _write_live_inputs, _sha256
    manifest_path, answers_path, manifest = _write_live_inputs(tmp_path)
    manifest["target"]["url"] = URL
    manifest["identity"] = {**IDENTITY, "platform": "greenhouse", "tenant": "schonfeld"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    answers = {key: ({"search_text": "Fixture", "exact_option": "Fixture"} if op == "react_select_exact" else "Fixture")
               for key, (_, op) in schonfeld_form.CONTROLS.items() if key != "resume"}
    answers.update(phone="5550100", gpa="3.236")
    answers_path.write_text(json.dumps(answers), encoding="utf-8")
    profile_path=Path(manifest['profile']['path'])
    accepted=json.loads(profile_path.read_text(encoding="utf-8"))
    accepted.update(name={'first':'Fixture'},contact={'email':'Fixture','phone':'5550100'},
        work_authorization=None,requires_sponsorship=None,education={'gpa':3.236},
        application_facts={k:v.get('exact_option') if isinstance(v,dict) else v for k,v in answers.items()})
    profile_path.write_text(json.dumps(accepted), encoding="utf-8")
    manifest['profile']['sha256']=_sha256(profile_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, answers_path, manifest, answers


def test_client_bound_one_page_uses_real_live_prepare_review_authorize_submit_contract(tmp_path):
    import production_operator as op
    import worker_operator
    from datetime import datetime, timezone
    from test_client_bound_review import client_inputs
    manifest_path, answers_path, manifest, answers = one_page_inputs(tmp_path)
    raw = client_inputs()["server_review"]
    raw.update(target_id=manifest["target"]["id"], page_url=URL, identity=IDENTITY,
        fields={selector: answers[key]["exact_option"] if isinstance(answers[key], dict) else answers[key]
                for key, (selector, _) in schonfeld_form.CONTROLS.items() if key != "resume"},
        parser_repairs=[], questions=[{"id": k,"required":True,"answered":True,"verified":True} for k in schonfeld_form.REQUIRED if k != "resume"])
    raw["bindings"] = {selector: {"count":1,"bound":True,"valid":True,"visible":True,"enabled":True,
        "choice": [{"value":"fixture","label":"Fixture"}] if operation == "react_select_exact" else None}
        for key,(selector,operation) in schonfeld_form.CONTROLS.items() if key != "resume"}
    raw["completeness"]["required_selectors"] = [schonfeld_form.CONTROLS[k][0] for k in schonfeld_form.REQUIRED if k != "resume"]
    raw["resume"].update(sha256=manifest["resume"]["sha256"])

    class Page(worker_operator.WorkerPage):
        def __init__(self):
            super().__init__(target_id=manifest["target"]["id"], target_url=URL, connection=None)
            self.clicks = 0
        def __exit__(self, *args): pass
        def read_only_snapshot(self):
            if self.clicks:
                return self.read_after_submit_snapshot()
            return {"target_id":self.target_id,"url":URL,"read_only":True,"html":HTML,"official_posting":POSTING,"gates":[]}
        def read_after_submit_snapshot(self):
            return {"target_id":self.target_id,"url":URL+'/thank_you',"read_only":True,
                "html":"<html><body><h1>Thank you for applying.</h1></body></html>","body_text":"Thank you for applying."}
        def _evaluate(self, expression):
            if "/* schonfeld:client-bound */" in expression and "/* schonfeld:submit */" not in expression:
                return copy.deepcopy(raw)
            if "/* schonfeld:submit */" in expression:
                if ".click()" in expression:
                    assert Path(manifest["runtime_paths"]["submit_journal"]).exists()
                    self.clicks += 1
                return {"visible":True,"enabled":True,"unique":True,"unobscured":True,"role":"button"}
            if expression == "window.devicePixelRatio": return 2
            if "file-upload__filename p" in expression: return "Resume.pdf"
            return {"count":1,"bound":True,"valid":True,"visible":True,"enabled":True,"unobscured":True,
                    "value": "5550100" if '"#phone"' in expression else "3.236" if '"#question_68930283"' in expression else "Fixture",
                    "choice":[{"value":"fixture","label":"Fixture"}]}
        def inspect_confirmation(self): return {"confirmed":False}
    page = Page()
    class Transport:
        def bind_mutable_page_target(self, target_id):
            assert target_id == page.target_id
            return page
    kwargs = dict(manifest_path=manifest_path, approved_answers_path=answers_path, step="application",
        cdp_base_url="http://127.0.0.1:9222", production_enabled=False, transport_factory=lambda _:Transport(),
        health_probe=lambda _: {"status":"ready"})
    from test_production_operator_live import _sha256
    profile_path=Path(manifest['profile']['path']); original_profile=profile_path.read_text(encoding="utf-8"); original_manifest=manifest_path.read_text(encoding="utf-8")
    wrong=json.loads(original_profile); wrong['education']['gpa']=3.8; profile_path.write_text(json.dumps(wrong), encoding="utf-8")
    changed_manifest=json.loads(original_manifest); changed_manifest['profile']['sha256']=_sha256(profile_path); manifest_path.write_text(json.dumps(changed_manifest), encoding="utf-8")
    with pytest.raises(ValueError, match='canonical profile'):
        op.run_live_prepare(**kwargs, coverage=lambda **_: {'human_required':[]})
    profile_path.write_text(original_profile, encoding="utf-8");manifest_path.write_text(original_manifest, encoding="utf-8")
    prepared = op.run_live_prepare(**kwargs, coverage=lambda **_: {"human_required":[{"question":"Opaque learned question label"}]})
    assert prepared["review_ready"] is True
    assert prepared["evidence"]["input_binding"]["profile_sha256"] == manifest["profile"]["sha256"]
    assert prepared["evidence"]["input_binding"]["resume_sha256"] == manifest["resume"]["sha256"]
    assert prepared["evidence"]["prepared_at"]
    original_answers = answers_path.read_text(encoding="utf-8")
    drifted = json.loads(original_answers); drifted["gpa"] = "9.999"
    answers_path.write_text(json.dumps(drifted), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical profile|input.*drift"):
        op.run_live_review(**kwargs, required_parser_repairs=[], required_question_ids=[])
    answers_path.write_text(original_answers, encoding="utf-8")
    preparation_path = Path(manifest["runtime_paths"]["preparation"])
    original_preparation = preparation_path.read_text(encoding="utf-8")
    stale = json.loads(original_preparation); stale["evidence"]["prepared_at"] = "2000-01-01T00:00:00+00:00"
    preparation_path.write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="fresh"):
        op.run_live_review(**kwargs, required_parser_repairs=[], required_question_ids=[])
    preparation_path.write_text(original_preparation, encoding="utf-8")
    reviewed, _ = op.run_live_review(**kwargs, required_parser_repairs=[], required_question_ids=[])
    assert reviewed["review"]["source"] == "live_client_bound_form"
    assert reviewed["review"]["server_saved"] is False
    assert reviewed["review"]["review_authoritative"] is True
    clock = lambda: datetime.now(timezone.utc)
    op.run_live_authorize(manifest_path=manifest_path, actor="offline test", approved_review_hash=reviewed["review"]["review_evidence_sha256"],
        expires_in_seconds=300, maango_approved=False, production_enabled=False, clock=clock)
    result = op.run_live_submit(**kwargs, required_parser_repairs=[], required_question_ids=[], actor="offline test",
        maango_approved=False, clock=clock)
    assert result["authorization_consumed"] is True
    assert result["replay_allowed"] is False
    assert page.clicks == 1
    confirmed, _ = op.run_live_confirmation(manifest_path=manifest_path, cdp_base_url=kwargs['cdp_base_url'],
        production_enabled=False, transport_factory=kwargs['transport_factory'], health_probe=kwargs['health_probe'])
    assert confirmed['portal']['portal_confirmed'] is True
    assert confirmed['portal']['portal_readback']['applicable'] is False
    with pytest.raises(ValueError):
        op.run_live_submit(**kwargs, required_parser_repairs=[], required_question_ids=[], actor="offline test",
            maango_approved=False, clock=clock)
    assert page.clicks == 1
    with pytest.raises(ValueError, match="intent|replay"):
        op.run_live_authorize(manifest_path=manifest_path, actor="offline test", approved_review_hash=reviewed["review"]["review_evidence_sha256"],
            expires_in_seconds=300, maango_approved=False, production_enabled=False, clock=clock)
    assert page.clicks == 1


def test_worker_wrapper_preserves_official_metadata_separately_from_page_html(tmp_path, capsys):
    import worker_operator
    from test_worker_operator import local_worker
    manifest_path, _, manifest, _ = one_page_inputs(tmp_path)
    posting_path=tmp_path/'posting.json'; posting_path.write_text(json.dumps(POSTING), encoding="utf-8")
    replies=[{"ok":True,"data":{"ready":True}}, {"ok":True,"data":[{"targetId":manifest["target"]["id"],"url":URL,"type":"page"}]}]
    replies += [{"ok":True,"data":{"binding":{"targetId":manifest["target"]["id"],"url":URL},"result":{"result":{"value":value}}}} for value in [URL,'Fixture', 'Fixture body',HTML]*2]
    with local_worker(replies) as (socket, requests):
        code=worker_operator.main(['--worker-socket',socket,'--posting-evidence',str(posting_path),'live','preflight','--manifest',str(manifest_path)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["identity_verified"] is True

