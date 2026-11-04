"""Synthetic newcomer setup, with no private profiles, services, or browser IO."""
import copy
import json

import pytest

import onboarding
from canonical_answers import (
    CanonicalAnswerError, canonical_review_fields, resolve_fact, verify_profile_answers,
)
from question_engine import QuestionAnswerEngine
from test_onboarding import Answers


NDA_QUESTIONS = [
    'Do you currently have any company NDAs?',
    'Do you currently have a company NDA?',
    'Do you currently have a non-disclosure agreement with any company?',
    'Do you have any company NDAs?',
    'Do you have any NDAs with any companies?',
    'Do you currently have an NDA with any company?',
    'Do you have a non-disclosure agreement with any company?',
    'Are you currently subject to a non-disclosure agreement with any company?',
    '  DO YOU CURRENTLY HAVE ANY COMPANY NDAS?*  ',
]


@pytest.mark.parametrize('question', NDA_QUESTIONS)
@pytest.mark.parametrize(('value', 'expected'), [(True, 'Yes'), (False, 'No'), ('Yes', 'Yes'), ('No', 'No')])
def test_current_company_nda_is_distinct_boolean_fact_reviewed_independently(question, value, expected):
    profile = {'application_facts': {'has_company_nda': value, 'restrictive_covenant': expected == 'No'}}
    result = QuestionAnswerEngine(profile=profile).answer(question)
    assert (result.status, result.question_key, result.answer) == ('answered', 'has_company_nda', expected)
    controls = {'has_company_nda': {'selector': '#nda', 'operation': 'native_select'}}
    assert canonical_review_fields(profile, controls) == {'#nda': expected}
    verify_profile_answers(profile, {'has_company_nda': expected})
    with pytest.raises(CanonicalAnswerError, match='conflicting'):
        verify_profile_answers(profile, {'has_company_nda': 'No' if expected == 'Yes' else 'Yes'})


@pytest.mark.parametrize('question', [
    'Have you ever signed an NDA with any company?',
    'Have you signed any company NDAs?',
    'Are you willing to sign a company NDA?',
    'Do you have an NDA with Example Corp?',
    'Do you have an NDA with this company?',
    'Do you have any company NDAs or restrictive covenants?',
    'Do you have any company NDAs? If so, explain.',
    'Do you have any NDAs?',  # Not necessarily a company agreement.
    'Do you have no company NDAs?',
])
def test_company_nda_does_not_ground_historical_future_specific_or_compound_asks(question):
    profile = {'application_facts': {'has_company_nda': False, 'restrictive_covenant': False}}
    result = QuestionAnswerEngine(profile=profile).answer(question, company='Example Corp')
    assert (result.status, result.question_key, result.answer) == ('unknown', 'unknown', None)


@pytest.mark.parametrize('value', ['', 'unknown', 'false', 'yes', 0, 1, [], {}])
def test_company_nda_rejects_malformed_boolean_values(value):
    profile = {'application_facts': {'has_company_nda': value}}
    with pytest.raises(CanonicalAnswerError, match='malformed'):
        resolve_fact(profile, 'has_company_nda')
    result = QuestionAnswerEngine(profile=profile).answer(NDA_QUESTIONS[0])
    assert (result.status, result.answer) == ('conflict', None)


@pytest.mark.parametrize('profile', [{}, {'application_facts': {'has_company_nda': None}},
                                       {'application_facts': {'restrictive_covenant': False}}])
def test_company_nda_is_not_inferred_from_other_legal_or_historical_answers(profile):
    engine = QuestionAnswerEngine(profile=profile, google_doc_answers=[
        {'question_key': 'has_company_nda', 'answer': 'No'},
    ])
    result = engine.answer(NDA_QUESTIONS[0])
    assert (result.status, result.reason, result.answer) == ('unknown', 'unknown_profile_fact', None)
    with pytest.raises(CanonicalAnswerError, match='missing'):
        canonical_review_fields(profile, {'has_company_nda': {'selector': '#nda'}})


PRESET_VALUES = {
    'application_facts.has_company_nda': False,
    'application_facts.restrictive_covenant': False,
    'screening_defaults.willing_to_relocate': True,
    'screening_defaults.requires_relocation_assistance': False,
    'screening_defaults.can_relocate_without_assistance': True,
}
PRESET_PROMPT = 'Use the early-career preset?'
FACT_PROMPTS = {
    'has_company_nda': NDA_QUESTIONS[0],
    'restrictive_covenant': 'Are you subject to a restrictive covenant (such as a non-compete)?',
    'willing_to_relocate': 'Are you willing to relocate?',
    'requires_relocation_assistance': 'Do you require relocation assistance?',
    'can_relocate_without_assistance': 'Can you relocate without assistance?',
}


def non_null_leaves(profile, prefix=''):
    leaves = {}
    for key, value in profile.items():
        path = f'{prefix}.{key}' if prefix else key
        if isinstance(value, dict):
            leaves.update(non_null_leaves(value, path))
        elif value is not None and value != []:
            leaves[path] = value
    return leaves


@pytest.mark.parametrize('reply', ['y', 'yes', 'YES'])
def test_new_profile_explicit_opt_in_prefills_only_five_reviewable_facts(reply):
    messages = []
    answers = Answers({PRESET_PROMPT: reply})
    profile = onboarding.collect_profile({}, input_fn=answers, output_fn=messages.append)
    answers.exhausted()
    assert non_null_leaves(profile) == PRESET_VALUES
    assert set(FACT_PROMPTS.values()).issubset(answers.prompts)
    text = '\n'.join(messages).lower()
    for phrase in ('early-career', 'company nda', 'restrictive covenant', 'willing to relocate',
                   'without assistance', 'yes, no, or unknown', 'review', 'save'):
        assert phrase in text
    for path, value in PRESET_VALUES.items():
        assert onboarding._review(profile)[path] is value


@pytest.mark.parametrize('reply', ['', 'no', 'n', 'sure', 'unknown'])
def test_new_profile_default_or_declined_preset_keeps_unknowns_and_custom_prompts(reply):
    answers = Answers({PRESET_PROMPT: reply})
    profile = onboarding.collect_profile({}, input_fn=answers, output_fn=lambda _: None)
    answers.exhausted()
    assert not non_null_leaves(profile)
    assert set(FACT_PROMPTS.values()).issubset(answers.prompts)
    engine = QuestionAnswerEngine(profile=profile)
    for field, question in FACT_PROMPTS.items():
        assert engine.answer(question).status == 'unknown'
        with pytest.raises(CanonicalAnswerError, match='missing'):
            resolve_fact(profile, field)


@pytest.mark.parametrize('save', ['', 'no', 'yes'])
@pytest.mark.parametrize('explicit_flag', [False, True])
def test_cli_preset_requires_final_review_and_explicit_save(tmp_path, save, explicit_flag):
    path = tmp_path / 'private.json'
    args = ['--profile', str(path), '--skip-credentials']
    if explicit_flag:
        args += ['--preset', 'early-career']
    messages = []
    answers = Answers({'Save this profile?': save, **({} if explicit_flag else {PRESET_PROMPT: 'yes'})})
    def scripted(prompt):
        if prompt.startswith('Save this profile?'):
            assert not path.exists()
            review = next(message for message in messages if message.startswith('Review'))
            reviewed = json.loads(review.split('\n', 1)[1])
            for key, value in PRESET_VALUES.items():
                assert reviewed[key] is value
        return answers(prompt)
    result = onboarding.main(args, input_fn=scripted, output_fn=messages.append)
    answers.exhausted()
    assert (PRESET_PROMPT in answers.prompts) is not explicit_flag
    assert result == (0 if save == 'yes' else 1)
    assert path.exists() is (save == 'yes')
    if save == 'yes':
        profile = json.loads(path.read_text(encoding="utf-8"))
        assert non_null_leaves(profile) == PRESET_VALUES
        expected = {path.split('.')[-1]: 'Yes' if value else 'No' for path, value in PRESET_VALUES.items()}
        engine = QuestionAnswerEngine(profile=profile)
        for field, question in FACT_PROMPTS.items():
            assert engine.answer(question).answer == expected[field]
        controls = {field: {'selector': '#' + field} for field in FACT_PROMPTS}
        assert canonical_review_fields(profile, controls) == {'#' + field: value for field, value in expected.items()}
        verify_profile_answers(profile, expected)

        # Saved generic willingness/funding facts cannot accept a particular location.
        commute = engine.answer(
            'Are you able to commute to the location listed on the requisition without relocation assistance?'
        )
        assert (commute.status, commute.question_key, commute.answer) == ('unknown', 'unknown', None)


def test_check_reports_new_optional_fields_without_applying_preset_or_prompting(tmp_path):
    path = tmp_path / 'private.json'
    path.write_text('{}', encoding="utf-8")
    original = path.read_bytes(), path.stat().st_mtime_ns
    messages = []
    def no_prompt(_):
        pytest.fail('Check must not prompt or apply a preset')
    assert onboarding.main(['--profile', str(path), '--check', '--preset', 'early-career'],
        input_fn=no_prompt, output_fn=messages.append) == 1
    for field in FACT_PROMPTS:
        assert f'{field}: missing / unknown / conflicting' in '\n'.join(messages)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


def test_custom_setup_can_answer_nda_and_assistance_without_a_preset():
    answers = Answers({FACT_PROMPTS['has_company_nda']: 'yes',
                       FACT_PROMPTS['requires_relocation_assistance']: 'yes',
                       FACT_PROMPTS['can_relocate_without_assistance']: 'no'})
    profile = onboarding.collect_profile({}, input_fn=answers, output_fn=lambda _: None)
    answers.exhausted()
    for field, expected in [('has_company_nda', 'Yes'), ('requires_relocation_assistance', 'Yes'),
                            ('can_relocate_without_assistance', 'No')]:
        assert resolve_fact(profile, field) == expected
    for field in ('restrictive_covenant', 'willing_to_relocate'):
        with pytest.raises(CanonicalAnswerError, match='missing'):
            resolve_fact(profile, field)


ALIASES = {field: (f'application_facts.{field}', f'screening_defaults.{field}') for field in FACT_PROMPTS}
ASSISTANCE_FIELDS = ('requires_relocation_assistance', 'can_relocate_without_assistance')


def fact_snapshot(profile, fields):
    return {path: onboarding._get(profile, path)
            for field in fields for path in ALIASES[field]
            if path.split('.')[1] in profile.get(path.split('.')[0], {})}


def enter_once():
    seen = set()
    def answer(prompt):
        assert prompt not in seen, 'Enter must preserve even malformed or conflicting existing facts'
        seen.add(prompt)
        return ''
    return answer


@pytest.mark.parametrize('path', [path for paths in ALIASES.values() for path in paths])
@pytest.mark.parametrize('value', [True, False, None, 'Yes', 'No', 'maybe', 0, {}])
def test_preset_preserves_existing_aliases_nulls_and_malformed_values_exactly(path, value):
    section, field = path.split('.')
    original = {section: {field: value}, 'unrelated': {'sentinel': [1, 2]}}
    before = copy.deepcopy(original)
    affected = ASSISTANCE_FIELDS if field in ASSISTANCE_FIELDS else (field,)
    profile = onboarding.collect_profile(original, preset='early-career', input_fn=enter_once(), output_fn=lambda _: None)
    assert fact_snapshot(profile, affected) == fact_snapshot(original, affected)
    assert profile['unrelated'] == original['unrelated']
    assert original == before
    for primary, default in PRESET_VALUES.items():
        if primary.split('.')[1] not in affected:
            assert onboarding._get(profile, primary) is default


@pytest.mark.parametrize('field', list(FACT_PROMPTS))
@pytest.mark.parametrize('malformed', [False, True])
def test_conflicting_aliases_survive_preset_and_are_visible_in_review(field, malformed):
    profile = {'application_facts': {field: True},
               'screening_defaults': {field: 'maybe' if malformed else False}}
    messages = []
    result = onboarding.collect_profile(profile, preset='early-career', input_fn=enter_once(), output_fn=messages.append)
    affected = ASSISTANCE_FIELDS if field in ASSISTANCE_FIELDS else (field,)
    assert fact_snapshot(result, affected) == fact_snapshot(profile, affected)
    with pytest.raises(CanonicalAnswerError, match='malformed' if malformed else 'conflicting'):
        resolve_fact(result, field)
    assert any('conflict' in message.lower() or 'invalid' in message.lower() for message in messages)
    review = onboarding._review(result)
    for path, value in fact_snapshot(profile, affected).items():
        assert review[path] == value


@pytest.mark.parametrize('field', ASSISTANCE_FIELDS)
@pytest.mark.parametrize('value', [True, False, None, 'maybe'])
def test_opposite_assistance_alias_blocks_both_preset_polarities(field, value):
    opposite = next(item for item in ASSISTANCE_FIELDS if item != field)
    existing = {'application_facts': {field: value}, 'screening_defaults': {opposite: value}}
    result = onboarding.collect_profile(existing, preset='early-career', input_fn=enter_once(), output_fn=lambda _: None)
    assert fact_snapshot(result, ASSISTANCE_FIELDS) == fact_snapshot(existing, ASSISTANCE_FIELDS)
    for key in ASSISTANCE_FIELDS:
        with pytest.raises(CanonicalAnswerError):
            resolve_fact(result, key)


@pytest.mark.parametrize('existing', [{}, {'unrelated': True},
    {'application_facts': {'has_company_nda': None}, 'screening_defaults': {'willing_to_relocate': False}}])
def test_existing_profile_cli_does_not_offer_or_silently_apply_preset(tmp_path, existing):
    path = tmp_path / 'private.json'
    path.write_text(json.dumps(existing), encoding="utf-8")
    answers = Answers({'Save this profile?': 'yes'})
    assert onboarding.main(['--profile', str(path), '--skip-credentials'],
        input_fn=answers, output_fn=lambda _: None) == 0
    assert PRESET_PROMPT not in answers.prompts
    saved = json.loads(path.read_text(encoding="utf-8"))
    for primary in PRESET_VALUES:
        assert onboarding._get(saved, primary) is onboarding._get(existing, primary)


@pytest.mark.parametrize('field', list(FACT_PROMPTS))
@pytest.mark.parametrize(('reply', 'expected'), [('yes', True), ('no', False), ('unknown', None), ('skip', None), ('?', None)])
def test_explicit_edit_reconciles_all_aliases_including_inverse_facts(field, reply, expected):
    existing = {'application_facts': {key: True for key in FACT_PROMPTS},
                'screening_defaults': {key: 'maybe' for key in FACT_PROMPTS}}
    original = copy.deepcopy(existing)
    once = enter_once()
    answers = Answers({FACT_PROMPTS[field]: reply})
    def scripted(prompt):
        once(prompt)
        return answers(prompt)
    profile = onboarding.collect_profile(existing, preset='early-career', input_fn=scripted, output_fn=lambda _: None)
    answers.exhausted()
    affected = ASSISTANCE_FIELDS if field in ASSISTANCE_FIELDS else (field,)
    for key in affected:
        primary = next(path for path in PRESET_VALUES if path.endswith('.' + key))
        value = None if expected is None else expected if key == field else not expected
        assert onboarding._get(profile, primary) is value
        if value is None:
            with pytest.raises(CanonicalAnswerError, match='missing'):
                resolve_fact(profile, key)
            assert QuestionAnswerEngine(profile=profile).answer(FACT_PROMPTS[key]).status == 'unknown'
        else:
            normalized = 'Yes' if value else 'No'
            assert resolve_fact(profile, key) == normalized
            assert canonical_review_fields(profile, {key: {'selector': '#field'}}) == {'#field': normalized}
            assert QuestionAnswerEngine(profile=profile).answer(FACT_PROMPTS[key]).answer == normalized
    unchanged = [key for key in FACT_PROMPTS if key not in affected]
    assert fact_snapshot(profile, unchanged) == fact_snapshot(existing, unchanged)
    assert existing == original


@pytest.mark.parametrize('field', list(FACT_PROMPTS))
def test_preset_unknown_clear_is_not_reintroduced_on_explicit_preset_rerun(field):
    result = onboarding.collect_profile({}, preset='early-career',
        input_fn=Answers({FACT_PROMPTS[field]: 'unknown'}), output_fn=lambda _: None)
    affected = ASSISTANCE_FIELDS if field in ASSISTANCE_FIELDS else (field,)
    rerun = onboarding.collect_profile(result, preset='early-career', input_fn=Answers(), output_fn=lambda _: None)
    for key in affected:
        with pytest.raises(CanonicalAnswerError, match='missing'):
            resolve_fact(result, key)
        with pytest.raises(CanonicalAnswerError, match='missing'):
            resolve_fact(rerun, key)
