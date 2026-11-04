"""Synthetic employer text is data; decoding must not turn it into tags."""
import json
from pathlib import Path
import pytest
from posting_qualifications import _Body, qualification_decision
from autonomous_controller import CandidateParked
from test_autonomous_backend import backend_with_candidate
from test_posting_qualifications import change_body


@pytest.mark.parametrize('escaped,visible', [
    ('&lt;U.S. citizenship required&gt;', '<U.S. citizenship required>'),
    ('&#60;U.S. citizenship required&#62;', '<U.S. citizenship required>'),
    ('&#x3c;Security clearance required&#x3e;', '<Security clearance required>'),
    ('&amp;lt;U.S. citizenship required&amp;gt;', '&lt;U.S. citizenship required&gt;'),
])
def test_entity_escaped_mandatory_clause_is_preserved_and_blocks(tmp_path, escaped, visible):
    body='<h2>Qualifications</h2><p>Minimum GPA of 3.0</p><p>'+escaped+'</p>'
    parser=_Body()
    parser.feed(body)
    parser.close()
    assert visible in ''.join(parser.parts)
    decision=qualification_decision({'content':body}, {'education':{'gpa':3.236}})
    assert decision['status']=='blocked_fact'
    assert {'constraint':'unsupported', 'verified':False} in decision['checks']
    backend,job=backend_with_candidate(tmp_path)
    change_body(backend,job,body)
    with pytest.raises(CandidateParked) as error:
        backend._canonical_inputs(job)
    assert error.value.state=='blocked_fact'


@pytest.mark.parametrize('body', [
    '&lt;p&gt;Minimum GPA of 3.0&lt;/p&gt;',
    '&#60;p&#62;Minimum GPA of 3.0&#60;/p&#62;',
    '&amp;lt;p&amp;gt;Minimum GPA of 3.0&amp;lt;/p&amp;gt;',
])
def test_encoded_document_is_not_guessed_to_be_html(body):
    decision=qualification_decision({'content':body}, {'education':{'gpa':3.236}})
    assert decision['status']=='blocked_fact'


def test_normal_character_references_are_decoded_once_as_text():
    decision=qualification_decision({'content':
        '<p>Minimum GPA of &#51;.0</p>'
        '<p>Must be currently enrolled in a bachelor&apos;s degree program</p>'},
        {'education':{'gpa':3.236, 'currently_enrolled':True, 'degree':'Bachelor of Science'}})
    assert decision['status']=='qualified'
    assert len(decision['checks'])==2


def test_fresh_recheck_rejects_old_hash_bound_decision_that_lost_visible_text(tmp_path):
    from posting_qualifications import content_hash
    from test_autonomous_backend import Response, FakeWorker
    from test_one_page_production import HTML
    backend,job=backend_with_candidate(tmp_path,client=FakeWorker())
    attempt=backend.build_inputs(job,{'id':'new-target','url':job.url},snapshot={
        'read_only':True,'target_id':'new-target','url':job.url,'html':HTML})
    path=Path(attempt['provenance_path'])
    saved=json.loads(path.read_text(encoding="utf-8"))
    posting={**saved['official_posting'], 'content':
        '<h2>Qualifications</h2><p>Minimum GPA of 3.0</p><p>&lt;U.S. citizenship required&gt;</p>'}
    # Exact buggy pre-fix decision, with correct raw-content/profile binding.
    old_decision={'version':1, 'posting_sha256':content_hash(posting),
        'profile_sha256':content_hash(json.loads(backend.profile_path.read_text(encoding="utf-8"))),
        'status':'qualified', 'reason':'explicit_canonical_requirements_matched',
        'checks':[{'constraint':'gpa','verified':True}]}
    saved.update(official_posting=posting, qualification_decision=old_decision)
    path.write_text(json.dumps(saved), encoding="utf-8")
    attempt['qualification_binding']=content_hash(old_decision)
    backend.opener=lambda *_:Response({'jobs':[posting]})
    with pytest.raises(CandidateParked) as error:
        backend._recheck_qualifications(attempt)
    assert error.value.state=='blocked_fact'
