"""Synthetic, offline onboarding flows; never load the user's live profile."""
import copy
import importlib
import json
from pathlib import Path
import pytest
import platform_io
from symlink_support import symlink_or_skip


def onboarding():
    return importlib.import_module("onboarding")


class Answers:
    """Script user answers by human-visible prompt, defaulting to Enter."""
    def __init__(self, values=None):
        self.values = {k: list(v) if isinstance(v, list) else [v]
                       for k, v in (values or {}).items()}
        self.prompts = []

    def __call__(self, prompt):
        label = prompt.split(" [", 1)[0].rstrip(": ")
        self.prompts.append(label)
        values = self.values.get(label, [])
        return values.pop(0) if values else ""

    def exhausted(self):
        assert not {k: v for k, v in self.values.items() if v}


def basic_answers(tmp_path):
    resume = tmp_path / "synthetic-resume.pdf"
    resume.write_bytes(b"%PDF-1.4 synthetic test fixture")
    return {
        "First name": "Alex", "Last name": "Synthetic",
        "Email": "alex@example.test", "Phone": "+1 555 010 1234",
        "City": "Exampleville", "State / region": "CA",
        "Country": "United States", "ZIP / postal code": "00000",
        "Resume file": str(resume), "School / university": "Example University",
        "Degree": "BS", "Major / field of study": "Computing",
        "GPA scale": "4", "GPA": "3.4", "Graduation date (YYYY-MM-DD)": "2027-06-15",
        "Graduation season": "Spring", "LinkedIn URL": "https://linkedin.com/in/synthetic",
        "GitHub URL": "https://github.com/synthetic",
    }


def test_collects_contact_resume_school_without_example_defaults(tmp_path):
    answers = Answers(basic_answers(tmp_path))
    messages = []
    result = onboarding().collect_profile({}, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    assert result["name"] == {"first": "Alex", "last": "Synthetic", "full": "Alex Synthetic"}
    assert result["contact"]["email"] == "alex@example.test"
    assert result["contact"]["location"]["city"] == "Exampleville"
    assert Path(result["resume"]["primary"]).is_file()
    assert result["education"]["university"] == "Example University"
    assert result["education"]["gpa"] == 3.4
    assert result["education"]["gpa_scale"] == 4.0
    assert result["education"]["graduation_date"] == "2027-06-15"
    assert result["education"]["expected_graduation"] == "2027"
    assert result["links"]["github"] == "https://github.com/synthetic"
    assert "Sam Fixture" not in json.dumps(result)
    assert "Basic contact" in "\n".join(messages)


def test_graduation_edit_updates_qualification_and_review_consumers():
    from canonical_answers import canonical_review_fields, resolve_fact
    from posting_qualifications import qualification_decision
    from schonfeld_form import verify_profile_answers

    existing = {
        "education": {"graduation_date": "2027-06-15", "expected_graduation": "2027",
                      "end_month": "June"},
        "application_facts": {"education_end_year": "2027", "education_end_month": "June"},
    }
    snapshot = copy.deepcopy(existing)
    result = onboarding().collect_profile(existing,
        input_fn=Answers({"Graduation date (YYYY-MM-DD)": "2028-12-15"}), output_fn=lambda _: None)
    assert resolve_fact(result, "education_end_year") == "2028"
    assert resolve_fact(result, "education_end_month") == "December"
    assert result["education"]["end_month"] == "December"
    controls = {
        "education_end_year": {"selector": "#end-year", "operation": "replace_text"},
        "education_end_month": {"selector": "#end-month", "operation": "react_select_exact"},
    }
    assert canonical_review_fields(result, controls) == {"#end-year": "2028", "#end-month": "December"}
    verify_profile_answers(result, {
        "education_end_year": "2028", "education_end_month": {"exact_option": "December"}})
    assert qualification_decision({"content": "Graduation between May 2028 and June 2028"}, result)["status"] == "blocked_fact"
    assert qualification_decision({"content": "Graduation between December 2028 and December 2028"}, result)["status"] == "qualified"
    assert existing == snapshot


def test_clearing_graduation_invalidates_qualification_and_review_consumers():
    from canonical_answers import CanonicalAnswerError, resolve_fact
    from posting_qualifications import qualification_decision
    from schonfeld_form import verify_profile_answers

    existing = {
        "education": {"graduation_date": "2028-12-15", "expected_graduation": "2028",
                      "end_month": "December"},
        "application_facts": {"education_end_year": "2028", "education_end_month": "December"},
    }
    result = onboarding().collect_profile(existing,
        input_fn=Answers({"Graduation date (YYYY-MM-DD)": "unknown"}), output_fn=lambda _: None)
    assert result["education"]["graduation_date"] is None
    assert result["education"].get("expected_graduation") is None
    assert result["education"].get("end_month") is None
    for field, old_value in (("education_end_year", "2028"), ("education_end_month", "December")):
        assert result["application_facts"].get(field) is None
        with pytest.raises(CanonicalAnswerError, match="missing"):
            resolve_fact(result, field)
        with pytest.raises(ValueError, match="missing or conflicting"):
            verify_profile_answers(result, {field: old_value})
    assert qualification_decision({"content": "Graduation between 2028 and 2029"}, result)["status"] == "blocked_fact"


def test_rerun_preserves_full_legal_name_and_middle_name():
    existing = {"name": {"first": "Alex", "middle": "Morgan", "last": "Synthetic",
                         "full": "Alex Morgan Synthetic"}}
    result = onboarding().collect_profile(existing, input_fn=Answers(), output_fn=lambda _: None)
    assert result["name"] == existing["name"]
    updated = onboarding().collect_profile(existing,
        input_fn=Answers({"Last name": "Changed"}), output_fn=lambda _: None)
    assert updated["name"]["full"] == "Alex Morgan Changed"


@pytest.mark.parametrize(('answers', 'expected'), [
    ({'First name': 'Jamie'}, 'Jamie Morgan Synthetic'),
    ({'Last name': 'Changed'}, 'Alex Morgan Changed'),
    ({'First name': 'unknown'}, 'Morgan Synthetic'),
    ({'First name': 'unknown', 'Last name': 'unknown'}, 'Morgan'),
])
def test_derived_full_name_change_clears_stale_alias_for_canonical_consumer(answers, expected):
    from canonical_answers import resolve_fact

    existing = {'name': {'first': 'Alex', 'middle': 'Morgan', 'last': 'Synthetic',
                         'full': 'Alex Morgan Synthetic'},
                'application_facts': {'full_name': 'Alex Morgan Synthetic'}}
    snapshot = copy.deepcopy(existing)
    result = onboarding().collect_profile(existing, input_fn=Answers(answers), output_fn=lambda _: None)
    assert result['name']['full'] == expected
    assert resolve_fact(result, 'full_name') == expected
    assert existing == snapshot


@pytest.mark.parametrize('answers', [{}, {'First name': 'Alex'}, {'Last name': 'Synthetic'}])
@pytest.mark.parametrize('alias', ['Alex Morgan Synthetic', 'Conflicting Full Name'])
def test_unchanged_derived_full_name_preserves_existing_alias(answers, alias):
    existing = {'name': {'first': 'Alex', 'middle': 'Morgan', 'last': 'Synthetic',
                         'full': 'Alex Morgan Synthetic'},
                'application_facts': {'full_name': alias}}
    result = onboarding().collect_profile(existing, input_fn=Answers(answers), output_fn=lambda _: None)
    assert result['name']['full'] == existing['name']['full']
    assert result['application_facts']['full_name'] == alias


def test_eligibility_uses_explicit_booleans_clears_legacy_aliases_without_mutation():
    existing = {"requires_sponsorship": True, "work_authorization": False,
        "screening_defaults": {"require_sponsorship": True, "authorized_to_work_us": False},
        "application_facts": {"sponsorship_now_or_future": True, "sponsorship": True,
                              "requires_sponsorship": True},
        "unrelated": {"keep": ["untouched"]}}
    snapshot = copy.deepcopy(existing)
    answers = Answers({
        "Are you a US citizen?": "no",
        "Are you authorized to work in the US?": "yes",
        "Do you need employer sponsorship now?": "no",
        "Will you need employer sponsorship in the future?": "yes",
        "Do you need OPT (Optional Practical Training)?": "yes",
        "Do you need CPT (Curricular Practical Training)?": "unknown",
    })
    messages = []
    result = onboarding().collect_profile(existing, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    facts = result["application_facts"]
    assert facts["us_citizen"] is False
    assert facts["sponsorship_now"] is False
    assert facts["sponsorship_future"] is True
    assert facts["needs_opt"] is True
    assert facts["needs_cpt"] is None
    assert not {"sponsorship_now_or_future", "sponsorship", "requires_sponsorship"} & facts.keys()
    assert "requires_sponsorship" not in result
    assert "require_sponsorship" not in result["screening_defaults"]
    assert result["work_authorization"] is result["screening_defaults"]["authorized_to_work_us"] is True
    assert result["unrelated"] == snapshot["unrelated"]
    assert existing == snapshot
    assert "not legal advice" in " ".join(messages).lower()


@pytest.mark.parametrize("value", [True, False, "Yes", "No"])
@pytest.mark.parametrize("source", ["screening_defaults.authorized_to_work_us",
    "application_facts.work_authorization", "application_facts.authorized_to_work_us"])
def test_enter_rerun_resolves_nonconflicting_authorization_aliases(source, value):
    from canonical_answers import resolve_fact
    from question_engine import QuestionAnswerEngine

    existing: dict = {"work_authorization": None}
    section, field = source.split(".")
    existing[section] = {field: value}
    snapshot = copy.deepcopy(existing)
    result = onboarding().collect_profile(existing, input_fn=Answers(), output_fn=lambda _: None)
    expected = value is True or value == "Yes"
    assert result["work_authorization"] is expected
    assert result["screening_defaults"]["authorized_to_work_us"] is expected
    assert resolve_fact(result, "work_authorization") == ("Yes" if expected else "No")
    assert QuestionAnswerEngine(profile=result).answer("Are you authorized to work in the US?").answer == ("Yes" if expected else "No")
    assert existing == snapshot


@pytest.mark.parametrize("existing", [
    {"work_authorization": False, "screening_defaults": {"authorized_to_work_us": True}},
    {"work_authorization": None, "screening_defaults": {"authorized_to_work_us": True},
     "application_facts": {"work_authorization": False}},
    {"work_authorization": True, "screening_defaults": {"authorized_to_work_us": None},
     "application_facts": {"authorized_to_work_us": False}},
    {"work_authorization": True, "screening_defaults": {"authorized_to_work_us": "maybe"}},
])
def test_conflicting_authorization_aliases_require_explicit_resolution(existing):
    from canonical_answers import CanonicalAnswerError, resolve_fact

    paths = ("work_authorization", "screening_defaults.authorized_to_work_us",
             "application_facts.work_authorization", "application_facts.authorized_to_work_us")
    snapshot = copy.deepcopy(existing)
    messages = []
    unchanged = onboarding().collect_profile(existing, input_fn=Answers(), output_fn=messages.append)
    assert {path: onboarding()._get(unchanged, path) for path in paths} == {
        path: onboarding()._get(existing, path) for path in paths}
    assert any("conflict" in message.lower() for message in messages)
    for reply, expected in (("yes", True), ("no", False), ("unknown", None)):
        result = onboarding().collect_profile(existing,
            input_fn=Answers({"Are you authorized to work in the US?": reply}), output_fn=lambda _: None)
        assert result["work_authorization"] is expected
        assert result["screening_defaults"]["authorized_to_work_us"] is expected
        assert not {"work_authorization", "authorized_to_work_us"} & result["application_facts"].keys()
        if expected is None:
            with pytest.raises(CanonicalAnswerError, match="missing"):
                resolve_fact(result, "work_authorization")
        else:
            assert resolve_fact(result, "work_authorization") == ("Yes" if expected else "No")
    assert existing == snapshot


@pytest.mark.parametrize('consumer', ['work_authorization', 'authorized_to_work_us', 'question_engine'])
def test_preserved_authorization_extension_conflict_fails_closed_for_consumers(consumer):
    from canonical_answers import CanonicalAnswerError, resolve_fact
    from question_engine import QuestionAnswerEngine

    existing = {'work_authorization': True, 'application_facts': {'authorized_to_work_us': False}}
    snapshot = copy.deepcopy(existing)
    result = onboarding().collect_profile(existing, input_fn=Answers(), output_fn=lambda _: None)
    assert result['work_authorization'] is True
    assert result['application_facts']['authorized_to_work_us'] is False
    assert existing == snapshot
    if consumer == 'question_engine':
        answer = QuestionAnswerEngine(profile=result).answer('Are you authorized to work in the US?')
        assert (answer.status, answer.answer) == ('conflict', None)
    else:
        with pytest.raises(CanonicalAnswerError, match='conflicting'):
            resolve_fact(result, consumer)


def test_authorization_conflict_is_preserved_when_editing_sponsorship_without_screening():
    existing = {"work_authorization": True, "application_facts": {"work_authorization": False}}
    result = onboarding().collect_profile(existing,
        input_fn=Answers({"Do you need employer sponsorship now?": "no"}), output_fn=lambda _: None)
    assert result["work_authorization"] is True
    assert result["application_facts"]["work_authorization"] is False
    assert result["application_facts"]["sponsorship_now"] is False


def test_enter_only_preferences_preserve_absence_and_scanner_classification():
    from scanner import classify

    existing = {"preferences": {"target_roles": ["Software Engineer Intern"]}}
    job = {"company": "Example", "role": "Software Engineer Intern, Summer 2027",
           "location": "Remote - United States", "url": "https://example.test/jobs/1"}
    before = classify(job, existing)
    result = onboarding().collect_profile(existing, input_fn=Answers(), output_fn=lambda _: None)
    assert result["preferences"] == existing["preferences"]
    assert classify(job, result) == before
    assert before["relevant"] is True


@pytest.mark.parametrize(("label", "field", "reply"), [
    ("Target roles", "target_roles", "unknown"),
    ("Target levels", "target_levels", "skip"),
    ("Target timelines", "target_timelines", "?"),
])
def test_explicit_unknown_preference_does_not_broaden_scanner_targeting(label, field, reply):
    from scanner import classify

    existing = {"preferences": {"target_roles": ["Software Engineer Intern"],
        "target_levels": ["Intern"], "target_timelines": ["Summer 2027"]}}
    job = {"company": "Example", "role": "Software Engineer Intern, Summer 2027",
           "location": "Remote - United States", "url": "https://example.test/jobs/1"}
    assert classify(job, existing)["relevant"] is True
    result = onboarding().collect_profile(existing,
        input_fn=Answers({label + " (comma-separated)": reply}), output_fn=lambda _: None)
    assert result["preferences"][field] is None
    classified = classify(job, result)
    assert classified["relevant"] is False
    assert classified["eligibility_status"] != "eligible"
    assert existing["preferences"][field] is not None
    rerun = onboarding().collect_profile(result, input_fn=Answers(), output_fn=lambda _: None)
    assert rerun["preferences"] == result["preferences"]
    assert classify(job, rerun) == classified


@pytest.mark.parametrize("label", [None, "Target roles", "Target levels", "Target timelines"])
def test_unset_preferences_without_target_roles_do_not_crash_scanner(label):
    from scanner import classify

    answers = Answers({label + " (comma-separated)": "unknown"} if label else {})
    result = onboarding().collect_profile({}, input_fn=answers, output_fn=lambda _: None)
    answers.exhausted()
    classified = classify({"company": "Example", "role": "Software Engineer Intern",
        "location": "Remote - United States", "url": "https://example.test/jobs/1"}, result)
    assert classified["relevant"] is False
    assert classified["eligibility_status"] != "eligible"


def test_preferences_conflicts_availability_and_optional_demographics_are_explicit():
    answers = Answers({
        "Do you have any company affiliations?": "no",
        "Are you subject to a restrictive covenant (such as a non-compete)?": "skip",
        "Are you at least 18 years old?": "yes", "Are you willing to relocate?": "no",
        "Do you have outside business activities?": "unknown",
        "Earliest available start date (YYYY-MM-DD)": "2027-07-01",
        "Full-time start date (YYYY-MM-DD)": "2027-08-01",
        "Desired salary (include currency and period; no default)": "$40/hour",
        "Target roles (comma-separated)": "Software Engineer, Data Analyst",
        "Target levels (comma-separated)": "Intern, Entry Level",
        "Target timelines (comma-separated)": "Summer 2027",
        "How did you hear about opportunities?": "Career fair",
        "Social media source (if applicable)": "skip",
        "Gender (optional)": "Prefer not to say",
        "Race / ethnicity (optional)": "skip", "Veteran status (optional)": "unknown",
        "Disability status (optional)": "?",
    })
    result = onboarding().collect_profile({}, input_fn=answers, output_fn=lambda _: None)
    answers.exhausted()
    facts = result["application_facts"]
    assert facts["has_company_affiliations"] is False
    assert facts["restrictive_covenant"] is None
    assert facts["available_start_date"] == "2027-07-01"
    assert facts["full_time_start_date"] == "2027-08-01"
    screening = result["screening_defaults"]
    assert screening["is_18_or_older"] is True
    assert screening["willing_to_relocate"] is False
    assert screening["outside_business_activities"] is None
    assert screening["desired_salary"] == "$40/hour"
    assert screening["demographic_disclosures"] == {
        "gender": "Prefer not to say", "race": None, "veteran": None, "disability": None}
    assert result["preferences"]["target_roles"] == ["Software Engineer", "Data Analyst"]
    assert result["preferences"]["target_levels"] == ["Intern", "Entry Level"]
    assert result["preferences"]["target_timelines"] == ["Summer 2027"]
    blank = onboarding().collect_profile({}, input_fn=Answers(), output_fn=lambda _: None)
    assert all(v is None for v in blank["application_facts"].values())
    assert blank["screening_defaults"]["desired_salary"] is None
    assert all(v is None for v in blank["screening_defaults"]["demographic_disclosures"].values())


def test_company_records_are_explicit_multiple_and_preserve_other_employers():
    original = {"company_disclosures": [{"company": "Other Employer", "former_employee": True}]}
    answers = Answers({
        "Do you have any company affiliations?": "no",
        "Exact company name (Enter to finish)": ["Acme Test", "Beta Test", ""],
        "Are you currently employed by Acme Test?": "no",
        "Were you formerly employed by Acme Test?": "yes",
        "Do you have relatives employed by Acme Test?": "unknown",
        "Affiliation details for Acme Test (optional)": "Previous internship",
        "Are you currently employed by Beta Test?": "unknown",
        "Were you formerly employed by Beta Test?": "no",
        "Do you have relatives employed by Beta Test?": "yes",
    })
    messages = []
    result = onboarding().collect_profile(original, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    assert result["company_disclosures"] == [original["company_disclosures"][0],
        {"company": "Acme Test", "current_employee": False, "former_employee": True,
         "relatives_employed": None, "affiliation_details": "Previous internship"},
        {"company": "Beta Test", "current_employee": None, "former_employee": False,
         "relatives_employed": True, "affiliation_details": None}]
    assert "does not answer" in " ".join(messages)
    update = Answers({"Exact company name (Enter to finish)": ["Acme Test", ""],
                      "Were you formerly employed by Acme Test?": "unknown"})
    edited = onboarding().collect_profile(result, input_fn=update, output_fn=lambda _: None)
    update.exhausted()
    assert edited["company_disclosures"][1]["former_employee"] is None
    assert edited["company_disclosures"][0] == result["company_disclosures"][0]
    assert edited["company_disclosures"][2] == result["company_disclosures"][2]
    assert result["company_disclosures"][1]["former_employee"] is True


@pytest.mark.parametrize(("label", "bad", "good", "path", "expected"), [
    ("Email", "not-an-email", "alex@example.test", "contact.email", "alex@example.test"),
    ("Resume file", "/definitely/missing/resume.pdf", "RESUME", "resume.primary", "RESUME"),
    ("Graduation date (YYYY-MM-DD)", "2027-02-30", "2027-06-15", "education.graduation_date", "2027-06-15"),
    ("Earliest available start date (YYYY-MM-DD)", "20270701", "2027-07-01", "application_facts.available_start_date", "2027-07-01"),
    ("Full-time start date (YYYY-MM-DD)", "tomorrow", "2027-08-01", "application_facts.full_time_start_date", "2027-08-01"),
    ("GPA", "nan", "3.4", "education.gpa", 3.4),
    ("GPA", "4.1", "3.4", "education.gpa", 3.4),
    ("GPA scale", "0", "4", "education.gpa_scale", 4.0),
    ("Are you a US citizen?", "maybe", "no", "application_facts.us_citizen", False),
])
def test_invalid_answers_reprompt_without_saving_bad_values(tmp_path, label, bad, good, path, expected):
    values = basic_answers(tmp_path)
    if good == "RESUME":
        good = expected = values["Resume file"]
    values[label] = [bad, good]
    answers = Answers(values)
    messages = []
    result = onboarding().collect_profile({}, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    for part in path.split("."):
        result = result[part]
    assert result == expected
    assert answers.prompts.count(label) == 2
    assert any("Invalid" in message for message in messages)


def test_editing_unknown_clears_stale_sensitive_aliases_and_preserves_unedited_values():
    existing = {
        "citizenship": "United States", "work_authorization": True,
        "application_facts": {"us_citizen": True, "authorized_to_work_us": True,
                              "work_authorization": True, "gender": "Female"},
        "gender": "Female", "veteran_status": "No", "race_ethnicity": "White", "disability": "No",
        "education": {"graduation_date": "2027-06-15", "expected_graduation": "2027"},
        "screening_defaults": {"authorized_to_work_us": True,
            "demographic_disclosures": {"gender": "Female", "veteran": "No", "race": "White", "disability": "No"}},
    }
    answers = Answers({"Are you a US citizen?": "unknown",
        "Are you authorized to work in the US?": "unknown",
        "Gender (optional)": "unknown", "Veteran status (optional)": "unknown",
        "Race / ethnicity (optional)": "unknown", "Disability status (optional)": "unknown",
        "Graduation date (YYYY-MM-DD)": "unknown"})
    messages = []
    result = onboarding().collect_profile(existing, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    assert result.get("citizenship") is None
    assert result["application_facts"]["us_citizen"] is None
    assert result["work_authorization"] is result["screening_defaults"]["authorized_to_work_us"] is None
    assert not {"authorized_to_work_us", "work_authorization", "gender"} & result["application_facts"].keys()
    assert all(result.get(key) is None for key in ("gender", "race_ethnicity", "veteran_status", "disability"))
    assert result["education"].get("expected_graduation") is None
    text = " ".join(messages)
    assert "OPT/CPT" in text and "DSO" in text and "not automatically" in text
    legacy = {"requires_sponsorship": True, "screening_defaults": {"authorized_to_work_us": False}}
    retained = onboarding().collect_profile(legacy, input_fn=Answers(), output_fn=lambda _: None)
    assert retained["requires_sponsorship"] is True
    assert retained["work_authorization"] is retained["screening_defaults"]["authorized_to_work_us"] is False


def test_cli_review_then_atomic_private_save_preserves_unrelated_data(tmp_path, monkeypatch):
    import os
    import stat
    path = tmp_path / "private.json"
    original = {"credentials": {"legacy": "NEVER_PRINT_SECRET"},
                "integration": {"token": "NEVER_PRINT_TOKEN"},
                "contact": {"private_note": "NEVER_PRINT_NOTE"}}
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    answers = Answers({**basic_answers(tmp_path), "Save this profile?": "yes"})
    messages = []
    def scripted(prompt):
        if prompt.startswith("Save this profile?"):
            assert path.read_bytes() == before
            assert "Review" in " ".join(messages)
            assert "Alex Synthetic" in " ".join(messages)
        return answers(prompt)
    real_replace = os.replace
    def observed_replace(source, destination):
        assert path.read_bytes() == before
        assert platform_io.is_private_file(Path(source))
        assert Path(source).parent == path.parent
        real_replace(source, destination)
    monkeypatch.setattr(os, "replace", observed_replace)
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=scripted, output_fn=messages.append) == 0
    answers.exhausted()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["name"]["full"] == "Alex Synthetic"
    assert saved["credentials"] == original["credentials"]
    assert saved["integration"] == original["integration"]
    assert saved["contact"]["private_note"] == "NEVER_PRINT_NOTE"
    assert "NEVER_PRINT" not in " ".join(messages)
    assert platform_io.is_private_file(path)
    assert {p.name for p in tmp_path.iterdir()} == {"private.json", "synthetic-resume.pdf"}


@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_cancel_or_interrupted_setup_never_modifies_profile(tmp_path, interrupt):
    path = tmp_path / "private.json"
    path.write_text('{"untouched": true}\n', encoding="utf-8")
    original = path.read_bytes()
    messages = []
    for stop_at in ("First name", "Save this profile?"):
        def interrupted(prompt):
            if prompt.startswith(stop_at):
                raise interrupt
            return ""
        assert onboarding().main(["--profile", str(path), "--skip-credentials"],
            input_fn=interrupted, output_fn=messages.append) == 1
        assert path.read_bytes() == original
    for reply in ("", "no", "sure"):
        assert onboarding().main(["--profile", str(path), "--skip-credentials"],
            input_fn=Answers({"Save this profile?": reply}), output_fn=messages.append) == 1
        assert path.read_bytes() == original
    path.unlink()
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=Answers(), output_fn=messages.append) == 1
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["tracked", "example", "symlink", "linked_directory"])
def test_refuses_unsafe_destinations_before_collecting_private_data(tmp_path, kind):
    import subprocess
    path = tmp_path / "private.json"
    path.write_text('{"sentinel": "NEVER_PRINT"}', encoding="utf-8")
    protected = path
    if kind == "tracked":
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", path.name], check=True)
    elif kind == "example":
        path = tmp_path / "profile.example.json"
        path.write_text(protected.read_text(encoding="utf-8"), encoding="utf-8")
        protected = path
    elif kind == "symlink":
        path = tmp_path / "link.json"
        symlink_or_skip(path, protected)
    else:
        link = tmp_path / "linked"
        symlink_or_skip(link, tmp_path, target_is_directory=True)
        path = link / "private.json"
    before = protected.read_bytes()
    def forbidden(_):
        pytest.fail("Unsafe destination must be rejected before any prompt")
    messages = []
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=forbidden, output_fn=messages.append) == 2
    assert protected.read_bytes() == before
    assert "NEVER_PRINT" not in " ".join(messages)
    assert "not saved" in " ".join(messages).lower()


def test_check_is_read_only_secret_free_and_optional_screening_is_not_required(tmp_path):
    path = tmp_path / "private.json"
    answers = basic_answers(tmp_path)
    profile = {"name": {"first": "Alex", "last": "Synthetic"},
        "contact": {"email": answers["Email"], "phone": answers["Phone"]},
        "resume": {"primary": answers["Resume file"]},
        "credentials": {"password": "NEVER_PRINT"}, "arbitrary": "NEVER_PRINT"}
    path.write_text(json.dumps(profile), encoding="utf-8")
    before = path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode
    messages = []
    def no_prompt(_):
        pytest.fail("check mode must not ask questions")
    assert onboarding().main(["--profile", str(path), "--check"],
        input_fn=no_prompt, output_fn=messages.append) == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_mode) == before
    text = "\n".join(messages)
    assert "Ready" in text and "Optional screening" in text
    assert "us_citizen" in text and "sponsorship_future" in text
    assert all(secret not in text for secret in ("NEVER_PRINT", answers["Email"], answers["Phone"], answers["Resume file"]))
    profile["contact"]["email"] = "invalid"
    path.write_text(json.dumps(profile), encoding="utf-8")
    assert onboarding().main(["--profile", str(path), "--check"], output_fn=messages.append) == 1
    assert "contact.email" in " ".join(messages)
    path.unlink()
    assert onboarding().main(["--profile", str(path), "--check"], output_fn=messages.append) == 1
    assert not path.exists()


def test_credentials_run_only_after_confirmation_and_save_references_only(tmp_path, monkeypatch):
    import sys
    import types
    path = tmp_path / "private.json"
    references = {kind: {"service": service, "account": "alex@example.test"} for kind, service in (
        ("universal", "hermes-job-agent-universal"), ("workday", "hermes-job-agent-workday-universal"))}
    messages = []
    called = []
    def setup(account, *, input_fn, output_fn):
        assert account == "alex@example.test"
        assert not path.exists()
        assert "Review" in " ".join(messages)
        called.append(account)
        return references
    monkeypatch.setitem(sys.modules, "onboarding_credentials", types.SimpleNamespace(setup_credentials=setup))
    answers = Answers({**basic_answers(tmp_path), "Save this profile?": "yes"})
    assert onboarding().main(["--profile", str(path)], input_fn=answers, output_fn=messages.append) == 0
    answers.exhausted()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["credentials"] == references
    assert called == ["alex@example.test"]
    assert "hermes-job-agent" not in " ".join(messages)
    path.unlink()
    assert onboarding().main(["--profile", str(path)], input_fn=Answers(), output_fn=lambda _: None) == 1
    assert called == ["alex@example.test"]


def test_credentials_eof_cancels_profile_even_when_helper_catches_it(tmp_path):
    path = tmp_path / "private.json"
    answers = Answers({**basic_answers(tmp_path), "Save this profile?": "yes"})
    def interrupted(prompt):
        if prompt.startswith("[s]kip"):
            raise EOFError
        return answers(prompt)
    assert onboarding().main(["--profile", str(path)], input_fn=interrupted, output_fn=lambda _: None) == 1
    assert not path.exists()


def test_rechecks_destination_after_review_before_replacing_anything(tmp_path):
    path = tmp_path / "private.json"
    protected = tmp_path / "protected.json"
    protected.write_text('{"unchanged": true}', encoding="utf-8")
    def change_destination(prompt):
        if prompt.startswith("Save this profile?"):
            symlink_or_skip(path, protected)
            return "yes"
        return ""
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=change_destination, output_fn=lambda _: None) == 2
    assert path.is_symlink()
    assert protected.read_text(encoding="utf-8") == '{"unchanged": true}'
    assert {p.name for p in tmp_path.iterdir()} == {"private.json", "protected.json"}


def test_company_edit_requires_named_employer_and_does_not_duplicate_case_variants():
    answers = Answers({"Exact company name (Enter to finish)": ["unknown", "*", "  ACME   TEST  ", ""],
                       "Are you currently employed by Acme Test?": "yes"})
    profile = {"company_disclosures": [{"company": "Acme Test", "current_employee": False}]}
    messages = []
    result = onboarding().collect_profile(profile, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    assert len(result["company_disclosures"]) == 1
    assert result["company_disclosures"][0]["company"] == "Acme Test"
    assert result["company_disclosures"][0]["current_employee"] is True
    assert any("exact company" in message.lower() for message in messages)


def test_rerun_boolean_answers_are_boolean_or_null_not_legacy_strings():
    existing = {"application_facts": {"us_citizen": "No", "needs_opt": "Yes", "needs_cpt": "maybe"},
                "screening_defaults": {"authorized_to_work_us": "Yes"}}
    answers = Answers({"Do you need CPT (Curricular Practical Training)?": ["", "unknown"]})
    result = onboarding().collect_profile(existing, input_fn=answers, output_fn=lambda _: None)
    answers.exhausted()
    assert result["application_facts"]["us_citizen"] is False
    assert result["application_facts"]["needs_opt"] is True
    assert result["application_facts"]["needs_cpt"] is None
    assert result["work_authorization"] is result["screening_defaults"]["authorized_to_work_us"] is True


@pytest.mark.parametrize("content", ['[]', 'null', '"PRIVATE_SENTINEL"', '{broken JSON', '{"company_disclosures": {}}'])
def test_malformed_profile_is_not_replaced_or_echoed(tmp_path, content):
    path = tmp_path / "private.json"
    path.write_text(content, encoding="utf-8")
    messages = []
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=Answers({"Save this profile?": "yes"}), output_fn=messages.append) == 2
    assert path.read_text(encoding="utf-8") == content
    assert "PRIVATE_SENTINEL" not in " ".join(messages)


def test_missing_credential_helper_is_lazy_and_has_safe_recovery(tmp_path, monkeypatch):
    import sys
    path = tmp_path / "private.json"
    monkeypatch.setitem(sys.modules, "onboarding_credentials", None)
    messages = []
    assert onboarding().main(["--profile", str(path)], input_fn=Answers({"Save this profile?": "yes"}),
        output_fn=messages.append) == 2
    assert not path.exists()
    assert "--skip-credentials" in " ".join(messages)
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=Answers({"Save this profile?": "yes"}), output_fn=messages.append) == 0
    assert path.exists()


def test_real_cli_saved_profile_roundtrips_through_question_answer_engine(tmp_path):
    import subprocess
    import sys
    from question_engine import QuestionAnswerEngine
    path = tmp_path / "private.json"
    values = {**basic_answers(tmp_path),
        "Are you a US citizen?": "no", "Are you authorized to work in the US?": "yes",
        "Do you need employer sponsorship now?": "no",
        "Will you need employer sponsorship in the future?": "yes",
        "Do you need OPT (Optional Practical Training)?": "yes",
        "Do you need CPT (Curricular Practical Training)?": "unknown",
        "Do you have any company affiliations?": "no",
        "Earliest available start date (YYYY-MM-DD)": "2027-07-01",
        "Full-time start date (YYYY-MM-DD)": "2027-08-01",
        "Target roles (comma-separated)": "Software Engineer",
        "Exact company name (Enter to finish)": ["Example Corp", ""],
        "Are you currently employed by Example Corp?": "no",
        "Were you formerly employed by Example Corp?": "yes",
        "Do you have relatives employed by Example Corp?": "unknown"}
    answers, lines = Answers(values), []
    def record(prompt):
        value = answers(prompt)
        lines.append(value)
        return value
    onboarding().collect_profile({}, input_fn=record, output_fn=lambda _: None)
    answers.exhausted()
    command = [sys.executable, onboarding().__file__, "--profile", str(path)]
    run = subprocess.run(command + ["--skip-credentials"], input="\n".join(lines + ["yes", ""]),
                         text=True, capture_output=True, cwd=tmp_path, timeout=20)
    assert run.returncode == 0, run.stderr
    profile = json.loads(path.read_text(encoding="utf-8"))
    engine = QuestionAnswerEngine(profile=profile)
    expected = {
        "Are you a US citizen?": "No", "Are you authorized to work in the US?": "Yes",
        "Do you require visa sponsorship?": "No", "Will you require sponsorship?": "Yes",
        "Do you need OPT?": "Yes", "Do you need CPT?": None,
        "Do you have any affiliations with any companies?": "No",
        "Are you subject to a restrictive covenant?": None,
        "What is your earliest available start date?": "2027-07-01",
        "When can you start full-time after graduation?": "2027-08-01",
        "What university do you attend?": "Example University",
        "What is your desired salary?": None, "What is your gender?": None,
    }
    for question, answer in expected.items():
        result = engine.answer(question)
        assert (result.status, result.answer) == ("unknown" if answer is None else "answered", answer), question
    assert engine.answer("Have you previously been employed by this company?", company="Example Corp").answer == "Yes"
    assert engine.answer("Do you have relatives employed by this company?", company="Example Corp").status == "unknown"
    assert engine.answer("Have you previously been employed by this company?", company="Different Corp").status == "unknown"
    before = path.read_bytes(), path.stat().st_mtime_ns
    check = subprocess.run(command + ["--check"], text=True, capture_output=True, cwd=tmp_path, timeout=20)
    assert check.returncode == 0 and "Ready" in check.stdout
    assert "alex@example.test" not in check.stdout
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_default_profile_path_is_next_to_script_not_current_directory(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    monkeypatch.setattr(onboarding(), "__file__", str(app / "onboarding.py"))
    monkeypatch.chdir(tmp_path)
    assert onboarding().main(["--skip-credentials"],
        input_fn=Answers({"Save this profile?": "yes"}), output_fn=lambda _: None) == 0
    assert (app / "profile.json").is_file()
    assert not (tmp_path / "profile.json").exists()


def test_failed_atomic_replace_leaves_original_and_no_backup(tmp_path, monkeypatch):
    import os
    path = tmp_path / "private.json"
    path.write_text('{"untouched": true}', encoding="utf-8")
    def fail_replace(*_):
        raise OSError("PRIVATE_ERROR_DETAIL")
    monkeypatch.setattr(os, "replace", fail_replace)
    messages = []
    assert onboarding().main(["--profile", str(path), "--skip-credentials"],
        input_fn=Answers({"Save this profile?": "yes"}), output_fn=messages.append) == 2
    assert path.read_text(encoding="utf-8") == '{"untouched": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["private.json"]
    assert "PRIVATE_ERROR_DETAIL" not in " ".join(messages)


def test_hidden_password_eof_also_cancels_profile_without_keychain_access(tmp_path, monkeypatch):
    import onboarding_credentials as credentials
    class EmptyBackend:
        def get_password(self, *_):
            return None
    def eof(_):
        raise EOFError
    real_setup = credentials._setup_credentials
    monkeypatch.setattr(credentials, "_setup_credentials",
        lambda account, input_fn, output_fn, password_fn, backend:
            real_setup(account, input_fn, output_fn, eof, EmptyBackend()))
    path = tmp_path / "private.json"
    answers = Answers({**basic_answers(tmp_path), "Save this profile?": "yes"})
    def scripted(prompt):
        return "n" if prompt.startswith("[s]kip") else answers(prompt)
    assert onboarding().main(["--profile", str(path)], input_fn=scripted, output_fn=lambda _: None) == 1
    assert not path.exists()


@pytest.mark.parametrize(("label", "field", "old", "reply", "expected"), [
    ("Do you have outside business activities?", "outside_business_activities", False, "unknown", None),
    ("Are you willing to relocate?", "willing_to_relocate", True, "no", "No"),
    ("Are you at least 18 years old?", "is_18_or_older", True, "unknown", None),
    ("Desired salary (include currency and period; no default)", "desired_salary", "$1/hour", "unknown", None),
    ("School / university", "school", "Old School", "New School", "New School"),
])
def test_explicit_edits_clear_related_application_fact_aliases(label, field, old, reply, expected):
    from canonical_answers import CanonicalAnswerError, resolve_fact
    profile = onboarding().collect_profile({"application_facts": {field: old}},
        input_fn=Answers({label: reply}), output_fn=lambda _: None)
    assert field not in profile["application_facts"]
    if expected is None:
        with pytest.raises(CanonicalAnswerError):
            resolve_fact(profile, field)
    else:
        assert resolve_fact(profile, field) == expected
