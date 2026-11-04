"""Synthetic Oracle shell regression; no real employer or browser interaction."""
import json
from prepare_job import main

URL = 'https://example.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/123'


def test_prepare_cli_rejects_unrecognized_oracle_page_without_controls(tmp_path, capsys):
    saved = tmp_path/'synthetic-shell.html'
    saved.write_text('<html><head><title>Careers</title></head><body><div id="root"></div></body></html>', encoding="utf-8")
    status = main([str(saved), '--page-url', URL])
    result = json.loads(capsys.readouterr().out)
    assert status == 2
    assert result['page_type'] == 'unknown'
    assert result['safe_to_prepare'] is False
    assert result['submission_enabled'] is False
    assert result['fields'] == []


def test_known_oracle_surfaces_do_not_gain_a_new_preparation_flag():
    from oracle_handler import inspect_html
    for html in ('<button class="apply-now-button">Apply Now</button>',
                 '<label for="answer">Synthetic question</label><input id="answer" required>'):
        result = inspect_html(html, page_url=URL)
        assert result['page_type'] in ('listing','application')
        assert 'safe_to_prepare' not in result


def test_empty_snapshot_keeps_metadata_resume_and_issue_evidence():
    from oracle_handler import inspect_html
    result = inspect_html('<h1>Synthetic role</h1><a href="#resume">Resume required</a>'
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Synthetic role"}</script>',
        page_url=URL, expected_resume_basename='Synthetic.pdf')
    assert result['page_type'] == 'unknown'
    assert result['safe_to_prepare'] is False
    assert result['role'] == 'Synthetic role'
    assert result['uploaded_resume_verified'] is False
    assert result['issues'] == [{'message':'Resume required','target':'resume'}]
    assert result['confirmation_text'] is None


def test_unlabelled_control_stays_unknown_not_silently_recognized():
    from oracle_handler import inspect_html
    result = inspect_html('<input id="orphan" required>', page_url=URL)
    assert result['fields'] == []
    assert result['page_type'] == 'unknown'
    assert result['safe_to_prepare'] is False


def test_existing_confirmation_precedence_is_preserved():
    from oracle_handler import inspect_html
    result = inspect_html('<h1>Application submitted</h1>', page_url=URL+'/confirmation')
    assert result['page_type'] == 'confirmation'
    assert 'safe_to_prepare' not in result
    assert result['confirmation_text']


def test_manifest_does_not_count_empty_shell_as_an_application(tmp_path):
    from ats_preflight import run_preflight_manifest
    saved=tmp_path/'synthetic.html'
    saved.write_text('<div id="root"></div>', encoding="utf-8")
    result=run_preflight_manifest([{'platform':'oracle','page_url':URL,'html_path':str(saved)}])
    assert result['summary']['application_count'] == 0
    assert result['results'][0]['safe_to_prepare'] is False
