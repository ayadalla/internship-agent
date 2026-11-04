"""Synthetic reduced Workday bootstrap shell from two public SWE pilot shapes."""
import json
from prepare_job import main, prepare_saved_html

URL = 'https://example.wd1.myworkdayjobs.com/en-US/Careers/job/Intern_R1'
SHELL = '''<!DOCTYPE html><html><head><title></title>
<meta property="og:title" content="Software Engineering Intern">
<script type="application/ld+json">{"@type":"JobPosting","title":"Software Engineering Intern"}</script>
<script>window.workday = {postingAvailable: true};</script>
</head><body><div id="root"></div></body></html>'''


def test_saved_prepare_cli_blocks_a_workday_shell_without_application_controls(tmp_path, capsys):
    source = tmp_path / 'synthetic-workday-shell.html'
    source.write_text(SHELL, encoding="utf-8")
    status = main([str(source), '--page-url', URL])
    result = json.loads(capsys.readouterr().out)
    assert status == 2
    assert result['page_type'] == 'unknown'
    assert result['safe_to_prepare'] is False
    assert result['fields'] == []
    assert result['confirmation_text'] is None
    assert result['submission_enabled'] is False


def test_legacy_workday_inspector_does_not_classify_empty_shell_as_application():
    from ats_preflight import inspect_workday_html
    result = inspect_workday_html(SHELL, page_url=URL)
    assert result['page_type'] == 'unknown'
    assert result['fields'] == []


def test_manifest_does_not_count_empty_shell_as_an_application(tmp_path):
    from ats_preflight import run_preflight_manifest
    source = tmp_path / 'synthetic-shell.html'
    source.write_text(SHELL, encoding="utf-8")
    result = run_preflight_manifest([{'platform':'workday', 'html_path':str(source), 'page_url':URL}])
    assert result['summary']['target_count'] == 1
    assert result['summary']['application_count'] == 0
    assert result['results'][0]['safe_to_prepare'] is False


def test_empty_or_unrecognized_markup_is_unknown_not_closed():
    for html in ('', '<html><body></body></html>', '<h1>Internship</h1>',
                 '<p>Loading</p>', SHELL.replace('true', 'false')):
        result = prepare_saved_html(html_text=html, page_url=URL)
        assert result['page_type'] == 'unknown'
        assert result['safe_to_prepare'] is False
        assert result['submission_enabled'] is False


def test_recognized_zero_control_surface_precedence_is_preserved():
    for markup, expected in (
        ('<button data-automation-id="adventureButton">Apply</button>', 'listing'),
        ('<a data-automation-id="applyManually" href="/apply/manual">Apply Manually</a>', 'application_start'),
        ('<p>Thank you for applying. Your application has been received.</p>', 'confirmation'),
    ):
        result = prepare_saved_html(html_text=markup, page_url=URL)
        assert result['page_type'] == expected
        assert result['fields'] == []
        assert result['safe_to_prepare'] is False
        assert result['submission_enabled'] is False


def test_unknown_surface_preserves_human_gate_and_resume_failure():
    result = prepare_saved_html(html_text=SHELL+'<p>Verify your identity to continue.</p>', page_url=URL,
                                expected_resume_basename='Synthetic.pdf')
    assert result['page_type'] == 'unknown'
    assert result['manual_gate']['type'] == 'identity_verification'
    assert result['uploaded_resume_verified'] is False
    assert result['safe_to_prepare'] is False


def test_native_and_custom_controls_are_not_mistaken_for_empty_surface():
    for control in ('<input name="email" type="email">', '<select name="country"></select>',
                    '<textarea name="answer"></textarea>', '<div role="combobox" aria-label="Country"></div>'):
        result = prepare_saved_html(html_text=control, page_url=URL)
        assert len(result['fields']) == 1
        assert result['page_type'] == 'application'
        assert result['submission_enabled'] is False
