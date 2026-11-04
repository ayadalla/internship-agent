"""Synthetic search-only employer shell, inspired by the ASM public pilot."""
import json

from prepare_job import main, prepare_saved_html


PAGE = '<h1>Software Engineering Intern</h1><form action="/search" method="GET"><input type="search" name="q"></form>'
URL = "https://job-boards.greenhouse.io/example/jobs/1"


def test_saved_prepare_cli_does_not_accept_site_search_as_application(tmp_path, capsys):
    source = tmp_path / "synthetic-shell.html"
    source.write_text(PAGE, encoding="utf-8")
    status = main([str(source), "--page-url", URL])
    result = json.loads(capsys.readouterr().out)
    assert status == 2
    assert result["page_type"] == "listing"
    assert result["safe_to_prepare"] is False
    assert result["submission_enabled"] is False
    assert result["fields"] == [{"name": "q", "type": "search", "label": "", "required": False}]
    assert result["form_evidence"] == {
        "source": "static_html", "status": "search_only", "rendering_verified": False,
    }


def test_manifest_preflight_does_not_count_search_only_page_as_application(tmp_path):
    from ats_preflight import run_preflight_manifest

    source = tmp_path / "synthetic-search.html"
    source.write_text(PAGE, encoding="utf-8")
    result = run_preflight_manifest([{"platform": "greenhouse", "html_path": str(source), "page_url": URL}])
    assert result["summary"]["application_count"] == 0
    assert result["results"][0]["page_type"] == "listing"
    assert result["results"][0]["form_evidence"]["status"] == "search_only"


def test_search_only_detection_leaves_mixed_candidate_controls_intact():
    for extra in ('<input name="email" type="email">', '<div role="combobox" aria-label="Country"></div>',
                  '<textarea name="statement"></textarea>', '<select name="office"></select>'):
        result = prepare_saved_html(html_text=PAGE + extra, page_url=URL)
        assert result["page_type"] == "application"
        assert "form_evidence" not in result
        assert len(result["fields"]) == 2


def test_search_only_detection_requires_explicit_search_form_semantics():
    for page in (PAGE.replace('/search', '/apply'), PAGE.replace('GET', 'POST'),
                 '<h1>Intern</h1><input type="search" name="school">', '<h1>Intern</h1>'):
        result = prepare_saved_html(html_text=page, page_url=URL)
        assert "form_evidence" not in result


def test_search_only_evidence_cannot_override_confirmation_or_security_gate():
    confirmed = prepare_saved_html(html_text=PAGE + '<p>Thank you for applying. Your application has been received.</p>', page_url=URL)
    assert confirmed["page_type"] == "confirmation"
    gated = prepare_saved_html(html_text=PAGE + '<p>Verify your identity to continue.</p>', page_url=URL)
    assert gated["manual_gate"]["type"] == "identity_verification"
    assert gated["safe_to_prepare"] is False
    assert confirmed["submission_enabled"] is gated["submission_enabled"] is False
