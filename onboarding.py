"""Local guided profile setup. No browser, OAuth, or example-profile copying."""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

import platform_io
from canonical_answers import CanonicalAnswerError, FACT_PATHS, INVERSE_FACTS, fact_paths, resolve_fact


EARLY_CAREER_PRESET = {
    "application_facts.has_company_nda": False,
    "application_facts.restrictive_covenant": False,
    "screening_defaults.willing_to_relocate": True,
    "screening_defaults.requires_relocation_assistance": False,
    "screening_defaults.can_relocate_without_assistance": True,
}


def _boolean(text):
    if text.lower() in ("y", "yes"):
        return True
    if text.lower() in ("n", "no"):
        return False
    raise ValueError("Enter yes, no, or unknown.")


def _validate(path, value, profile):
    if value is None:
        return value
    if path == "contact.email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Enter an email address.")
    if path == "resume.primary":
        file = Path(value).expanduser()
        if not file.is_file():
            raise ValueError("Choose an existing resume file.")
        return str(file.resolve())
    if path.endswith("_date"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use YYYY-MM-DD.")
        date.fromisoformat(value)
    if path in ("education.gpa", "education.gpa_scale"):
        value = float(value)
        scale = _get(profile, "education.gpa_scale")
        if not math.isfinite(value) or value < 0 or (path.endswith("scale") and value == 0):
            raise ValueError("Use a finite nonnegative GPA and positive scale.")
        if path == "education.gpa" and scale is not None and value > float(scale):
            raise ValueError("GPA cannot exceed its scale.")
    return value


def _get(profile, path):
    value = profile
    for key in path.split("."):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _set(profile, path, value):
    parts = path.split(".")
    for key in parts[:-1]:
        if not isinstance(profile.get(key), dict):
            profile[key] = {}
        profile = profile[key]
    profile[parts[-1]] = value


def _present(profile, path):
    """Presence, not truthiness: explicit unknown is not a missing fact."""
    for key in path.split("."):
        if not isinstance(profile, dict) or key not in profile:
            return False
        profile = profile[key]
    return True


def _remove(profile, path):
    parts = path.split(".")
    for key in parts[:-1]:
        profile = profile.get(key) if isinstance(profile, dict) else None
    if isinstance(profile, dict):
        profile.pop(parts[-1], None)


def collect_profile(existing, *, preset=None, offer_preset=None, input_fn=input, output_fn=print):
    """Return an edited copy; the caller decides whether to persist it."""
    profile = copy.deepcopy(existing)
    changed = set()
    reconciled = set()
    if preset not in (None, "early-career"):
        raise ValueError("Unknown preset")
    if offer_preset is None:
        offer_preset = not existing
    if preset or offer_preset:
        output_fn("Optional early-career preset (reviewable, not a statement about every newcomer):\n"
                  "  Current company NDAs: No. Restrictive covenants (such as non-competes): No.\n"
                  "  Willing to relocate: Yes. Requires relocation assistance: No.\n"
                  "  Can relocate without assistance, at your own expense: Yes.\n"
                  "These are separate legal and relocation facts; no other answers are inferred.\n"
                  "Existing answers, including unknowns and conflicts, are never replaced by the preset.\n"
                  "Edit each answer with yes, no, or unknown. Review before the final save confirmation.")
        if preset is None and input_fn("Use the early-career preset? [y/N]: ").strip().lower() in ("y", "yes"):
            preset = "early-career"
    if preset:
        for path, value in EARLY_CAREER_PRESET.items():
            field = path.split(".")[-1]
            # Check the original snapshot: an existing alias, even null,
            # blocks both polarities, while both absent defaults can be filled.
            if not any(_present(existing, alias) for alias, _ in fact_paths(field)):
                _set(profile, path, value)

    def ask(path, label, convert: Callable = str, target=None, *, preserve_missing=False):
        target = profile if target is None else target
        old = _get(target, path)
        while True:
            raw = input_fn(f"{label} [{old if old is not None else 'unknown'}]: ").strip()
            try:
                value = None if raw.lower() in ("unknown", "skip", "?") else convert(raw) if raw else old
                if convert is _boolean and value is not None and not isinstance(value, bool):
                    value = _boolean(str(value))
                value = _validate(path, value, profile)
                break
            except (ValueError, TypeError, OSError):
                output_fn(f"Invalid answer for {label}; use the requested format, or unknown to skip.")
        if raw or value is not None or not preserve_missing:
            _set(target, path, value)
        if raw:
            changed.add(path)
        return value

    def ask_fact(path, label):
        """Enter preserves every alias; an explicit edit replaces the semantic fact."""
        field = path.split(".")[-1]
        paths = fact_paths(field)
        try:
            old = resolve_fact(profile, field)
        except CanonicalAnswerError as exc:
            old = "unknown"
            if "conflicting" in str(exc) or "malformed" in str(exc):
                old = "conflicting / invalid"
                output_fn(f"Existing {field} aliases conflict or are invalid; Enter preserves them. "
                          "Answer yes, no, or unknown to resolve explicitly.")
        while True:
            raw = input_fn(f"{label} [{old}]: ").strip()
            if not raw:
                if not any(_present(profile, alias) for alias, _ in paths):
                    _set(profile, path, None)
                return
            try:
                value = None if raw.lower() in ("unknown", "skip", "?") else _boolean(raw)
                break
            except ValueError:
                output_fn(f"Invalid answer for {label}; enter yes, no, or unknown.")
        for alias, _ in paths:
            _remove(profile, alias)
        _set(profile, path, value)
        changed.add(path)
        reconciled.add(field)
        inverse = INVERSE_FACTS.get(field)
        if inverse:
            inverse_path = next(key for key in EARLY_CAREER_PRESET if key.endswith("." + inverse))
            _set(profile, inverse_path, None if value is None else not value)
            changed.add(inverse_path)
            reconciled.add(inverse)

    output_fn("Enter keeps existing answers; unknown / skip / ? clears an answer. No implied No.")
    output_fn("Confidential answers stay in your local profile. This is not legal advice; skip uncertain answers.")
    output_fn("Basic contact, resume, and education")
    for path, label in (
        ("name.first", "First name"), ("name.last", "Last name"),
        ("contact.email", "Email"), ("contact.phone", "Phone"),
        ("contact.location.city", "City"), ("contact.location.state", "State / region"),
        ("contact.location.country", "Country"), ("contact.location.zip", "ZIP / postal code"),
        ("resume.primary", "Resume file"), ("education.university", "School / university"),
        ("education.degree", "Degree"), ("education.major", "Major / field of study"),
        ("links.linkedin", "LinkedIn URL"), ("links.github", "GitHub URL"),
    ):
        ask(path, label)
    previous_full_name = _get(profile, "name.full")
    if not previous_full_name or changed.intersection({"name.first", "name.last"}):
        _set(profile, "name.full", " ".join(filter(None, (
            _get(profile, "name.first"), _get(profile, "name.middle"), _get(profile, "name.last")
        ))) or None)
        if _get(profile, "name.full") != previous_full_name:
            changed.add("name.full")
    ask("education.gpa_scale", "GPA scale", float)
    ask("education.gpa", "GPA", float)
    graduation_date = ask("education.graduation_date", "Graduation date (YYYY-MM-DD)")
    if graduation_date:
        _set(profile, "education.expected_graduation", str(graduation_date)[:4])
    if "education.graduation_date" in changed:
        from calendar import month_name
        year = str(graduation_date)[:4] if graduation_date else None
        month = month_name[int(str(graduation_date)[5:7])] if graduation_date else None
        # Qualification and Review consumers read different legacy aliases.
        _set(profile, "education.expected_graduation", year)
        _set(profile, "education.end_month", month)
        _set(profile, "application_facts.education_end_year", year)
        _set(profile, "application_facts.education_end_month", month)
    ask("education.graduation_season", "Graduation season")
    output_fn("Work eligibility — answer yes, no, or unknown")
    output_fn("OPT/CPT is not automatically employer sponsorship. Do not infer an answer; consult your school DSO if unsure.")
    for field, label in (
        ("us_citizen", "Are you a US citizen?"),
        ("sponsorship_now", "Do you need employer sponsorship now?"),
        ("sponsorship_future", "Will you need employer sponsorship in the future?"),
        ("needs_opt", "Do you need OPT (Optional Practical Training)?"),
        ("needs_cpt", "Do you need CPT (Curricular Practical Training)?"),
    ):
        ask("application_facts." + field, label, _boolean)
    if "application_facts.us_citizen" in changed:
        profile.pop("citizenship", None)
    authorization_values = [_get(profile, path) for path in (
        "work_authorization", "screening_defaults.authorized_to_work_us",
        "application_facts.work_authorization", "application_facts.authorized_to_work_us")]
    try:
        known_authorizations = {value if isinstance(value, bool) else _boolean(str(value))
                                for value in authorization_values if value is not None}
        authorization_conflict = len(known_authorizations) > 1
    except ValueError:
        known_authorizations = set()
        authorization_conflict = True
    if authorization_conflict:
        output_fn("Existing work-authorization aliases conflict or are invalid; Enter preserves them. "
                  "Answer yes, no, or unknown to resolve explicitly.")
    authorization_answer = {"work_authorization":
        next(iter(known_authorizations)) if len(known_authorizations) == 1 else None}
    authorized = ask("work_authorization", "Are you authorized to work in the US?", _boolean,
                     target=authorization_answer)
    if "work_authorization" in changed or not authorization_conflict:
        _set(profile, "work_authorization", authorized)
        _set(profile, "screening_defaults.authorized_to_work_us", authorized)
    facts = profile["application_facts"]
    if "work_authorization" in changed:
        for alias in ("work_authorization", "authorized_to_work_us"):
            facts.pop(alias, None)
    if any("application_facts." + field in changed for field in ("sponsorship_now", "sponsorship_future")):
        profile.pop("requires_sponsorship", None)
        profile.get("screening_defaults", {}).pop("require_sponsorship", None)
        for alias in ("sponsorship_now_or_future", "sponsorship", "requires_sponsorship"):
            facts.pop(alias, None)
    output_fn("Conflicts, affiliations, and preferences")
    output_fn("Company NDAs are separate from restrictive covenants; neither answers historical signing "
              "or willingness to sign. Answer yes, no, or unknown.")
    output_fn("Relocation assistance and ability to relocate without assistance are inverse facts: "
              "editing or clearing either updates both, not willingness or your current address.")
    for path, label in (
        ("application_facts.has_company_affiliations", "Do you have any company affiliations?"),
        ("application_facts.has_company_nda", "Do you currently have any company NDAs?"),
        ("application_facts.restrictive_covenant", "Are you subject to a restrictive covenant (such as a non-compete)?"),
        ("screening_defaults.is_18_or_older", "Are you at least 18 years old?"),
        ("screening_defaults.willing_to_relocate", "Are you willing to relocate?"),
        ("screening_defaults.requires_relocation_assistance", "Do you require relocation assistance?"),
        ("screening_defaults.can_relocate_without_assistance", "Can you relocate without assistance?"),
        ("screening_defaults.outside_business_activities", "Do you have outside business activities?"),
    ):
        if path in EARLY_CAREER_PRESET:
            ask_fact(path, label)
        else:
            ask(path, label, _boolean)
    for path, label in (
        ("application_facts.available_start_date", "Earliest available start date (YYYY-MM-DD)"),
        ("application_facts.full_time_start_date", "Full-time start date (YYYY-MM-DD)"),
        ("screening_defaults.desired_salary", "Desired salary (include currency and period; no default)"),
        ("screening_defaults.how_did_you_hear", "How did you hear about opportunities?"),
        ("screening_defaults.social_media_source", "Social media source (if applicable)"),
    ):
        ask(path, label)
    for field, label in (("target_roles", "Target roles"), ("target_levels", "Target levels"),
                         ("target_timelines", "Target timelines")):
        ask("preferences." + field, label + " (comma-separated)",
            lambda text: [item.strip() for item in text.split(",") if item.strip()], preserve_missing=True)
    output_fn("A global affiliation answer does not answer employer-specific employment or relatives questions.")
    records = profile.setdefault("company_disclosures", [])
    output_fn("Add/update an exact company; Enter finishes. Existing companies: " +
              ", ".join(record["company"] for record in records))
    while True:
        company = input_fn("Exact company name (Enter to finish): ").strip()
        if not company:
            break
        if company.casefold() in ("unknown", "skip", "?", "all", "any") or "*" in company:
            output_fn("Enter an exact company name, or Enter to finish without a record.")
            continue
        normalized = " ".join(company.casefold().split())
        record = next((item for item in records if " ".join(item["company"].casefold().split()) == normalized), None)
        if record is None:
            record = {"company": company}
            records.append(record)
        company = record["company"]
        for field, label in (
            ("current_employee", f"Are you currently employed by {company}?"),
            ("former_employee", f"Were you formerly employed by {company}?"),
            ("relatives_employed", f"Do you have relatives employed by {company}?"),
        ):
            ask(field, label, _boolean, record)
        ask("affiliation_details", f"Affiliation details for {company} (optional)", target=record)
    output_fn("Demographics are optional; nothing is inferred.")
    for field, label in (("gender", "Gender"), ("race", "Race / ethnicity"),
                         ("veteran", "Veteran status"), ("disability", "Disability status")):
        path = "screening_defaults.demographic_disclosures." + field
        ask(path, label + " (optional)")
        if path in changed:
            profile.pop({"race": "race_ethnicity", "veteran": "veteran_status"}.get(field, field), None)
            facts.pop(field, None)
    for field, paths in FACT_PATHS.items():
        # Reconciled facts already contain authoritative edits; popping their
        # application_facts entries would erase the user's explicit answers.
        if field not in reconciled and changed.intersection(paths):
            facts.pop(field, None)
    return profile


def _review(profile):
    paths = ("name.first name.last name.full contact.email contact.phone contact.location.city "
        "contact.location.state contact.location.country contact.location.zip resume.primary "
        "education.university education.degree education.major education.gpa education.gpa_scale "
        "education.graduation_date education.expected_graduation education.graduation_season "
        "links.linkedin links.github work_authorization requires_sponsorship citizenship "
        "application_facts.us_citizen application_facts.needs_opt application_facts.needs_cpt "
        "application_facts.sponsorship_now application_facts.sponsorship_future "
        "application_facts.has_company_affiliations application_facts.restrictive_covenant "
        "application_facts.has_company_nda "
        "application_facts.available_start_date application_facts.full_time_start_date "
        "screening_defaults.is_18_or_older screening_defaults.willing_to_relocate "
        "screening_defaults.requires_relocation_assistance screening_defaults.can_relocate_without_assistance "
        "screening_defaults.outside_business_activities screening_defaults.desired_salary "
        "screening_defaults.how_did_you_hear screening_defaults.social_media_source "
        "screening_defaults.demographic_disclosures.gender screening_defaults.demographic_disclosures.race "
        "screening_defaults.demographic_disclosures.veteran screening_defaults.demographic_disclosures.disability "
        "preferences.target_roles preferences.target_levels preferences.target_timelines").split()
    review = {path: _get(profile, path) for path in paths}
    for primary in EARLY_CAREER_PRESET:
        for path, _ in fact_paths(primary.split(".")[-1]):
            if _present(profile, path):
                review[path] = _get(profile, path)
    review["company_disclosures"] = [{key: row.get(key) for key in
        ("company", "current_employee", "former_employee", "relatives_employed", "affiliation_details")}
        for row in profile.get("company_disclosures", [])]
    return review


def _safe_destination(path):
    if "example" in path.name.lower() or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Unsafe destination")
    tracked = subprocess.run(["git", "-C", str(path.parent), "ls-files", "--error-unmatch", "--", path.name],
                             capture_output=True, check=False)
    if tracked.returncode == 0:
        raise ValueError("Tracked destination")


def _save(path, profile):
    fd, temporary = tempfile.mkstemp(prefix=".onboarding-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            platform_io.make_private(temporary, stream.fileno())
            json.dump(profile, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_profile(profile, *, output_fn=print):
    """Report field names/statuses only; optional screening never blocks readiness."""
    from canonical_answers import CanonicalAnswerError, resolve_fact
    missing = []
    for path in ("name.first", "name.last", "contact.email", "contact.phone", "resume.primary"):
        value = _get(profile, path)
        try:
            if not isinstance(value, str) or not value.strip():
                raise ValueError
            _validate(path, value, profile)
        except (ValueError, TypeError, OSError):
            missing.append(path)
    output_fn("Not ready — missing/invalid essentials: " + ", ".join(missing) if missing
              else "Ready — essential contact and resume complete.")
    output_fn("Optional screening (unknown answers are not No):")
    for field in ("us_citizen", "work_authorization", "sponsorship_now", "sponsorship_future", "needs_opt",
                  "needs_cpt", "has_company_affiliations", "restrictive_covenant", "is_18_or_older",
                  "has_company_nda", "requires_relocation_assistance", "can_relocate_without_assistance",
                  "willing_to_relocate", "outside_business_activities", "available_start_date",
                  "full_time_start_date", "desired_salary", "gender", "race", "veteran", "disability"):
        try:
            resolve_fact(profile, field)
            status = "provided"
        except CanonicalAnswerError:
            status = "missing / unknown / conflicting"
        output_fn(f"  {field}: {status}")
    output_fn("Employer-specific disclosures require exact company records. No Sheets, OAuth, or browser setup required.")
    return 1 if missing else 0


def main(argv=None, *, input_fn=input, output_fn=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path(__file__).resolve().parent / "profile.json")
    parser.add_argument("--skip-credentials", action="store_true")
    parser.add_argument("--preset", choices=("early-career",), help="Prefill reviewable early-career answers, without replacing existing facts")
    parser.add_argument("--check", action="store_true", help="Read-only readiness checklist; no values printed")
    args = parser.parse_args(argv)
    path = args.profile.expanduser().absolute()
    try:
        if not args.check:
            _safe_destination(path)
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(existing, dict) or not isinstance(existing.get("company_disclosures", []), list):
            raise ValueError("Invalid profile shape")
        if args.check:
            return check_profile(existing, output_fn=output_fn)
        profile = collect_profile(existing, preset=args.preset, offer_preset=not path.exists(),
                                  input_fn=input_fn, output_fn=output_fn)
        output_fn("Review (credentials and unrelated settings omitted):\n" + json.dumps(_review(profile), indent=2))
        if input_fn("Save this profile? [y/N]: ").strip().lower() not in ("y", "yes"):
            raise EOFError
        _safe_destination(path)
        if not args.skip_credentials:
            from onboarding_credentials import setup_credentials
            cancelled = False
            def credential_input(prompt):
                nonlocal cancelled
                try:
                    return input_fn(prompt)
                except (EOFError, KeyboardInterrupt):
                    cancelled = True
                    raise
            def credential_output(message):
                nonlocal cancelled
                # The helper reports hidden-input cancellation before returning {} (also its skip result).
                if message.startswith("Credential setup cancelled."):
                    cancelled = True
                output_fn(message)
            references = setup_credentials(_get(profile, "contact.email"), input_fn=credential_input, output_fn=credential_output)
            if cancelled:
                raise EOFError
            if references:
                profile.setdefault("credentials", {}).update(references)
        _save(path, profile)
    except (EOFError, KeyboardInterrupt):
        output_fn("Cancelled; no profile saved.")
        return 1
    except ImportError:
        output_fn("Credential setup unavailable; profile not saved. Install dependencies or rerun with --skip-credentials.")
        return 2
    except (OSError, ValueError):
        output_fn("Profile not saved. Use a valid, untracked private JSON file, not an example or symlink.")
        return 2
    output_fn("Profile saved privately (owner-only access).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
