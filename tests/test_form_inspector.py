"""Offline tests for the read-only form inspector (a fake connection stands in for Chrome)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import form_inspector as fi

PAGE = {"id": "T1", "title": "Apply", "url": "https://job-boards.greenhouse.io/example/jobs/1"}
LISTED = {"id": "T1", "type": "page", "title": "Apply", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/T1",
          "url": "https://job-boards.greenhouse.io/example/jobs/1?gh_src=SECRET-TOKEN"}


class FakeConnection:
    """Answers the two fixed evaluations; records every call made through it."""

    def __init__(self, payload, hrefs):
        self.payload, self.hrefs, self.calls, self.closed = payload, list(hrefs), [], False

    def call(self, method, params):
        self.calls.append((method, params["expression"]))
        if params["expression"] == fi.LOCATION_EXPRESSION:
            return {"result": {"value": self.hrefs.pop(0)}}
        return {"result": {"value": json.dumps(self.payload)}}

    def close(self):
        self.closed = True


def payload(*controls):
    return {"title": "Apply", "ready_state": "complete", "truncated": False, "controls": list(controls)}


def control(**kw):
    base = {"tag": "input", "type": "text", "role": "", "id": "", "name": "", "label": "", "group": "",
            "required": False, "disabled": False, "read_only": False, "visible": True, "multiple": False}
    return {**base, **kw}


# ---- read-only enforcement ------------------------------------------------

@pytest.mark.parametrize("method", ["Input.dispatchMouseEvent", "Input.insertText", "DOM.setFileInputFiles",
                                    "Page.navigate", "Runtime.callFunctionOn", "DOM.scrollIntoViewIfNeeded"])
def test_read_only_connection_refuses_every_method_but_evaluate(method):
    inner = FakeConnection(payload(), [])
    with pytest.raises(fi.InspectorError, match="not allowed"):
        fi.ReadOnlyConnection(inner).call(method, {"expression": fi.LOCATION_EXPRESSION, "returnByValue": True})
    assert inner.calls == []


@pytest.mark.parametrize("expression", ["document.querySelector('#x').click()", "location.href='https://evil.test'",
                                        fi.INSPECT_EXPRESSION + ";1"])
def test_read_only_connection_refuses_any_other_expression(expression):
    inner = FakeConnection(payload(), [])
    with pytest.raises(fi.InspectorError, match="fixed"):
        fi.ReadOnlyConnection(inner).call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    assert inner.calls == []


def test_read_only_connection_refuses_extra_parameters():
    inner = FakeConnection(payload(), [])
    with pytest.raises(fi.InspectorError, match="parameters"):
        fi.ReadOnlyConnection(inner).call(
            "Runtime.evaluate", {"expression": fi.LOCATION_EXPRESSION, "returnByValue": True, "userGesture": True})


def test_inspection_script_never_reads_values_or_acts_on_the_page():
    script = fi.INSPECT_EXPRESSION
    for forbidden in (".value", ".files", ".click(", ".focus(", ".scrollIntoView", "dispatchEvent",
                      ".submit(", "location.href=", "innerText", "XMLHttpRequest", "fetch(", ".dispatch"):
        assert forbidden not in script, forbidden
    assert "getAttribute('value')" in script  # option identity of radios/checkboxes only


# ---- listing and choosing pages ------------------------------------------

def test_list_pages_keeps_only_http_pages_and_strips_query_strings():
    targets = [LISTED, {"id": "W", "type": "service_worker", "url": "https://x.test/"},
               {"id": "B", "type": "page", "url": "about:blank"}, {"id": "C", "type": "page", "url": "chrome://newtab/"}]
    pages = fi.list_pages("http://127.0.0.1:9222", fetch=lambda url: targets)
    assert pages == [{"id": "T1", "title": "Apply", "url": "https://job-boards.greenhouse.io/example/jobs/1"}]
    assert "SECRET-TOKEN" not in json.dumps(pages)


@pytest.mark.parametrize("base", ["http://evil.example:9222", "https://127.0.0.1:9222", "http://user:pw@127.0.0.1:9222",
                                  "http://127.0.0.1:9222/json", "http://192.168.1.5:9222"])
def test_only_a_plain_loopback_http_origin_is_accepted(base):
    with pytest.raises(fi.InspectorError, match="loopback"):
        fi.list_pages(base, fetch=lambda url: [])


def test_choose_page_requires_exactly_one_match():
    pages = [{"id": "1", "url": "https://a.test/x"}, {"id": "2", "url": "https://b.test/y"}]
    assert fi.choose_page(pages, "b.test")["id"] == "2"
    with pytest.raises(fi.InspectorError, match="more than one"):
        fi.choose_page(pages, None)
    with pytest.raises(fi.InspectorError, match="no open page"):
        fi.choose_page(pages, "nope")


# ---- categories and report ------------------------------------------------

@pytest.mark.parametrize("fields, expected", [
    (dict(id="first_name"), "standard"), (dict(id="school--0"), "standard"), (dict(id="start-month--2"), "standard"),
    (dict(id="candidate-location"), "standard"),
    (dict(id="question_55", label="LinkedIn"), "custom_question"),
    (dict(id="963", label="Gender"), "eeo_demographic"),
    (dict(id="question_9", label="Veteran status"), "eeo_demographic"),
    (dict(id="question_7[]_1", type="checkbox", label="I consent to the privacy policy"), "consent"),
    (dict(id="x", tag="button", type="submit"), "button"),
    (dict(id="tok", type="hidden"), "hidden"),
    (dict(id="mystery"), "other"),
])
def test_categorize_is_conservative(fields, expected):
    assert fi.categorize(control(**fields)) == expected


def test_report_summary_and_unlabeled_logic():
    report = fi.build_report(payload(
        control(id="first_name", label="First Name*", required=True),
        control(id="resume", type="file", group="Resume/CV*", required=True),
        control(id="mystery"),
        control(id="country", role="combobox", label="Country", options=None, options_verified=False),
        control(id="go", tag="button", type="submit"),
    ), page_url="https://job-boards.greenhouse.io/example/jobs/1?token=SECRET", inspected_at="2026-11-07T00:00:00+00:00")
    assert report["read_only"] is True and report["values_recorded"] is False
    assert report["page_url"] == "https://job-boards.greenhouse.io/example/jobs/1"
    assert report["summary"]["controls"] == 5 and report["summary"]["required"] == 2
    assert report["summary"]["unlabeled_visible"] == 1          # only "mystery"; resume has a group label
    assert report["summary"]["options_unverified"] == 1
    assert "SECRET" not in json.dumps(report)


def test_build_report_rejects_a_malformed_payload():
    with pytest.raises(fi.InspectorError):
        fi.build_report({"title": "x"}, page_url="https://a.test/", inspected_at="t")


# ---- full flow with a fake Chrome ----------------------------------------

def run(payload_, hrefs, monkeypatch=None):
    holder = {}

    def connect(ws):
        holder["conn"] = FakeConnection(payload_, hrefs)
        return holder["conn"]

    report = fi.inspect_page("http://127.0.0.1:9222", PAGE, fetch=lambda url: [LISTED], connect=connect)
    return report, holder["conn"]


def test_inspect_page_makes_only_the_three_fixed_evaluations_and_closes():
    href = LISTED["url"]
    report, conn = run(payload(control(id="first_name", label="First Name*")), [href, href])
    assert [expr for _, expr in conn.calls] == [fi.LOCATION_EXPRESSION, fi.INSPECT_EXPRESSION, fi.LOCATION_EXPRESSION]
    assert {method for method, _ in conn.calls} == {"Runtime.evaluate"}
    assert conn.closed is True
    assert report["summary"]["controls"] == 1


def test_inspect_page_aborts_if_the_page_navigated_during_inspection():
    with pytest.raises(fi.InspectorError, match="navigated"):
        run(payload(control(id="a")), [LISTED["url"], "https://job-boards.greenhouse.io/other/jobs/2"])


def test_inspect_page_aborts_if_the_page_changed_before_inspection():
    with pytest.raises(fi.InspectorError, match="changed between"):
        run(payload(control(id="a")), ["https://job-boards.greenhouse.io/other/jobs/2"] * 2)


def test_inspect_page_reports_a_missing_target():
    with pytest.raises(fi.InspectorError, match="not found"):
        fi.inspect_page("http://127.0.0.1:9222", {**PAGE, "id": "GONE"}, fetch=lambda url: [LISTED],
                        connect=lambda ws: FakeConnection(payload(), []))


def test_output_path_is_outside_the_repo_by_default_and_has_no_query_text(tmp_path):
    report = fi.build_report(payload(control(id="a")), page_url="https://job-boards.greenhouse.io/example/jobs/1?t=SECRET",
                             inspected_at="2026-11-07T01:02:03+00:00")
    path = fi.output_path(tmp_path, report)
    assert path.parent == tmp_path and "SECRET" not in path.name and path.suffix == ".json"
    repo = Path(__file__).resolve().parents[1]
    assert repo not in fi.default_output_dir().parents
