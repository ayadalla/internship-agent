from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.mark.parametrize(
    "platform,page_url,tenant,field_key,selector",
    [
        ("greenhouse", "https://job-boards.greenhouse.io/fixture/jobs/123", "fixture", "first_name", "#first_name"),
        ("greenhouse", "https://job-boards.greenhouse.io/embed/job_app?for=thetradedesk&token=5187605007", "thetradedesk", "school", "[id^='school--']"),
        ("greenhouse", "https://job-boards.greenhouse.io/c3ascend/jobs/8739036002", "c3ascend", "school", "input[id^='school--']"),
        ("workday", "https://fixture.wd1.myworkdayjobs.com/job/123", "fixture", "email", "#wd_email"),
        ("lever", "https://jobs.lever.co/fixture/123/apply", "fixture", "full_name", "#name"),
        ("oracle", "https://careers.example.test/job/123/apply", "example", "first_name", "#first_name"),
        ("njoyn", "https://cgi.njoyn.com/xweb?jobid=123", "cgi", "privacy_acknowledged", "[name='privacy_acknowledged']"),
    ],
)
def test_versioned_registry_resolves_exact_learned_tenant_controls(
    platform, page_url, tenant, field_key, selector
):
    import tenant_field_maps

    mapping = tenant_field_maps.resolve_field_map(page_url=page_url, platform=platform)
    controls = {
        key: control
        for step in mapping["steps"].values()
        for key, control in step["controls"].items()
    }

    assert mapping["version"] == 1
    assert mapping["platform"] == platform
    assert mapping["tenant"] == tenant
    assert controls[field_key]["selector"] == selector


def test_mapped_actions_reject_raw_selector_or_observed_control_drift():
    import tenant_field_maps

    mapping = tenant_field_maps.resolve_field_map(
        page_url="https://job-boards.greenhouse.io/fixture/jobs/123",
        platform="greenhouse",
    )

    with pytest.raises(tenant_field_maps.FieldMapError, match="unknown semantic field"):
        tenant_field_maps.build_step_actions(
            mapping=mapping,
            step="application",
            approved_answers={"#first_name": "Fixture"},
            observed_selectors={"#first_name"},
        )
    with pytest.raises(tenant_field_maps.ControlDriftError, match="learned control drift"):
        tenant_field_maps.build_step_actions(
            mapping=mapping,
            step="application",
            approved_answers={"first_name": "Fixture"},
            observed_selectors={"#different"},
        )


def test_trade_desk_field_map_exposes_generic_react_select_contract():
    import tenant_field_maps

    mapping = tenant_field_maps.resolve_field_map(
        page_url="https://job-boards.greenhouse.io/embed/job_app?for=thetradedesk&token=5187605007",
        platform="greenhouse",
    )

    actions = tenant_field_maps.build_step_actions(
        mapping=mapping,
        step="application",
        approved_answers={
            "school": {
                "search_text": "San Diego",
                "exact_option": "University of California - San Diego",
            }
        },
        observed_selectors={"[id^='school--']"},
    )

    assert actions == [{
        "field": "school",
        "operation": "react_select_exact",
        "selector": "[id^='school--']",
        "value": {
            "search_text": "San Diego",
            "exact_option": "University of California - San Diego",
        },
    }]


def test_c3_ascend_map_covers_required_application_and_consent_fields():
    import tenant_field_maps

    mapping = tenant_field_maps.resolve_field_map(
        page_url="https://job-boards.greenhouse.io/c3ascend/jobs/8739036002",
        platform="greenhouse",
    )

    controls = mapping["steps"]["application"]["controls"]
    assert controls["phone"] == {
        "selector": "#phone",
        "operation": "replace_tel_local_digits",
    }
    assert controls["location"] == {
        "selector": "#candidate-location",
        "operation": "react_select_exact",
    }
    assert controls["school"] == {
        "selector": "input[id^='school--']",
        "operation": "react_select_exact",
    }
    assert controls["privacy_accept"] == {
        "selector": "#question_37929970002\\[\\]_252235310002",
        "operation": "set_checked",
    }
    assert controls["sms_consent"] == {
        "selector": "#question_37929971002",
        "operation": "react_select_exact",
    }

    country_action = tenant_field_maps.build_step_actions(
        mapping=mapping,
        step="application",
        approved_answers={
            "country": {
                "search_text": "United States",
                "exact_option": "United States +1",
                "selected_option": "+1",
            }
        },
    )[0]
    assert country_action["value"] == {
        "search_text": "United States",
        "exact_option": "United States +1",
        "selected_option": "+1",
    }


def test_execute_react_select_action_uses_exact_option_contract():
    import tenant_field_maps

    class Page:
        def __init__(self):
            self.selected = ""
            self.operations = []

        def read_only_snapshot(self):
            return {
                "read_only": True,
                "target_id": "page-42",
                "url": "https://job-boards.greenhouse.io/fixture/jobs/123",
            }

        def react_select_exact(self, selector, search_text, exact_option):
            self.operations.append((selector, search_text, exact_option))
            self.selected = exact_option

        def read_react_selected_option(self, selector):
            return self.selected

    page = Page()
    result = tenant_field_maps.execute_step_actions(
        page=page,
        target_id="page-42",
        expected_url="https://job-boards.greenhouse.io/fixture/jobs/123",
        actions=[{
            "field": "school",
            "operation": "react_select_exact",
            "selector": "[id^='school--']",
            "value": {
                "search_text": "San Diego",
                "exact_option": "University of California - San Diego",
            },
        }],
    )

    assert page.operations == [(
        "[id^='school--']",
        "San Diego",
        "University of California - San Diego",
    )]
    assert result["verified"] is True


def test_conditional_steps_advance_only_after_required_actions_and_parser_evidence():
    import tenant_field_maps

    workday = tenant_field_maps.resolve_field_map(
        page_url="https://fixture.wd1.myworkdayjobs.com/job/123",
        platform="workday",
    )
    assert tenant_field_maps.plan_next_step(
        mapping=workday,
        current_step="my_information",
        completed_fields={"email", "phone"},
        conditions={},
    ) == {
        "status": "advance",
        "current_step": "my_information",
        "next_step": "experience",
        "human_required": [],
    }

    njoyn = tenant_field_maps.resolve_field_map(
        page_url="https://cgi.njoyn.com/xweb?jobid=123",
        platform="njoyn",
    )
    blocked = tenant_field_maps.plan_next_step(
        mapping=njoyn,
        current_step="parsed_profile",
        completed_fields={"first_name", "school"},
        conditions={"parser_repairs_verified": False},
    )
    assert blocked["status"] == "human_required"
    assert blocked["next_step"] == "parsed_profile"
    assert blocked["human_required"] == ["parser_repairs_verified"]
    assert tenant_field_maps.plan_next_step(
        mapping=njoyn,
        current_step="parsed_profile",
        completed_fields={"first_name", "school"},
        conditions={"parser_repairs_verified": True},
    )["next_step"] == "referral"


def test_prepare_live_job_default_path_uses_semantic_learned_control_map(tmp_path, capsys):
    import prepare_live_job

    page_url = "https://job-boards.greenhouse.io/fixture/jobs/123"
    fixture_html = (ROOT / "fixtures" / "greenhouse.html").read_text(encoding="utf-8")

    class Page:
        target_id = "page-42"

        def __init__(self):
            self.values = {"#first_name": ""}
            self.operations = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read_only_snapshot(self):
            return {
                "target_id": "page-42",
                "url": page_url,
                "html": fixture_html,
                "read_only": True,
            }

        def replace_text(self, selector, value):
            self.operations.append(("replace_text", selector, value))
            self.values[selector] = value

        def read_value(self, selector):
            return self.values[selector]

    page = Page()

    class Transport:
        def __init__(self, base_url):
            self.base_url = base_url

        def bind_mutable_page_target(self, target_id):
            assert target_id == "page-42"
            return page

    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    answers_path = tmp_path / "approved.json"
    answers_path.write_text(json.dumps({"first_name": "Fixture Person"}), encoding="utf-8")
    output_path = tmp_path / "review.json"

    exit_code = prepare_live_job.main(
        [
            "--target-id", "page-42",
            "--expected-url", page_url,
            "--company", "Fixture Company",
            "--role", "Software Engineer Intern",
            "--requisition", "123",
            "--platform", "greenhouse",
            "--step", "application",
            "--profile", str(profile_path),
            "--approved-answers", str(answers_path),
            "--output", str(output_path),
        ],
        transport_factory=Transport,
        coverage=lambda **kwargs: {
            "known": [],
            "company_specific": [],
            "optional_skip": [],
            "human_required": [],
        },
    )

    assert exit_code == 0
    assert page.operations == [("replace_text", "#first_name", "Fixture Person")]
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted["applied_answers"]["field_evidence"][0]["field"] == "first_name"
    assert "Fixture Person" not in output_path.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out) == persisted
