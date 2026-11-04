"""Non-submitting exact-target live preparation orchestration.

The command seam deliberately accepts an already bound page adapter. It never
navigates, submits, reaches credentials, tracker, or notification services. Its
only durable result is sanitized Review-ready evidence.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urlsplit

from answer_coverage import build_coverage_matrix
from cdp_page_executor import CDPPageExecutor
from prepare_job import prepare_saved_html
from scoped_cdp import ScopedCDPTransport
from tenant_field_maps import build_step_actions, execute_step_actions, resolve_field_map


class ReadOnlyLivePage(Protocol):
    target_id: str

    def read_only_snapshot(self) -> dict[str, object]: ...


class LivePreparationError(ValueError):
    """Live preparation cannot safely proceed without exact identity evidence."""


Prepare = Callable[..., dict]
Coverage = Callable[..., dict]
TransportFactory = Callable[[str], object]


def _identity(payload: dict) -> dict[str, str]:
    return {key: str(payload.get(key, "")) for key in ("company", "role", "requisition")}


def _resolve_verified_mapped_coverage(
    answer_coverage: dict, applied_answers: dict
) -> dict:
    """Resolve an unknown required control only after mapped browser read-back."""
    unresolved = answer_coverage.get("human_required", [])
    field_evidence = applied_answers.get("field_evidence", [])
    if not isinstance(unresolved, list) or not isinstance(field_evidence, list):
        return answer_coverage
    verified = [
        item
        for item in field_evidence
        if isinstance(item, dict)
        and item.get("verified") is True
        and isinstance(item.get("field"), str)
        and isinstance(item.get("selector"), str)
    ]
    remaining = []
    for blocker in unresolved:
        question = blocker.get("question") if isinstance(blocker, dict) else None
        match = next(
            (
                item
                for item in verified
                if isinstance(question, str)
                and question
                and (
                    item["selector"].replace("\\[", "[").replace("\\]", "]") == "#" + question
                    or (
                        question.endswith("[]")
                        and re.fullmatch(
                            "#" + re.escape(question) + r"_\d+",
                            item["selector"].replace("\\[", "[").replace("\\]", "]"),
                        ) is not None
                    )
                )
            ),
            None,
        )
        if match is None:
            remaining.append(blocker)
            continue
        answer_coverage.setdefault("known", []).append({
            "question": question,
            "question_key": match["field"],
            "source": "verified_approved_answer",
        })
    answer_coverage["human_required"] = remaining
    return answer_coverage


def prepare_live_job(
    *,
    page: ReadOnlyLivePage,
    target_id: str,
    expected_url: str,
    expected_identity: dict[str, str],
    profile: dict,
    prepare: Prepare,
    coverage: Coverage,
    approved_answers: dict[str, object] | None = None,
    apply_known: Callable[[dict[str, object]], dict[str, object]] | None = None,
) -> dict:
    """Inspect one fresh exact target and emit non-submitting Review evidence."""
    snapshot = page.read_only_snapshot()
    if snapshot.get("read_only") is not True or snapshot.get("target_id") != target_id:
        raise LivePreparationError("exact trusted target binding is required")
    if snapshot.get("url") != expected_url:
        raise LivePreparationError("target URL changed before live preparation")
    html = snapshot.get("html")
    if not isinstance(html, str):
        raise LivePreparationError("exact target did not return sanitized HTML evidence")

    prepared = prepare(html_text=html, page_url=expected_url)
    identity = _identity(prepared)
    expected = {key: str(expected_identity.get(key, "")) for key in identity}
    if identity != expected:
        raise LivePreparationError("company, role, or requisition changed before live preparation")
    if prepared.get("submission_enabled") is not False:
        raise LivePreparationError("live preparation must remain non-submitting")
    if prepared.get("gates") or snapshot.get("gates"):
        raise LivePreparationError("rendered application gate blocks preparation")

    questions = prepared.get("questions", [])
    if not isinstance(questions, list):
        raise LivePreparationError("handler returned invalid question inventory")
    answer_coverage = coverage(profile=profile, questions=questions, company=identity["company"])
    answers = dict(approved_answers or {})
    if answers and apply_known is None:
        raise LivePreparationError("approved answers require an exact-target apply operation")
    applied_answers = apply_known(answers) if answers else {
        "action": "fill_known_page", "field_evidence": [], "verified": True,
    }
    if applied_answers.get("verified") is not True:
        raise LivePreparationError("approved answer read-back was not verified")
    if answers:
        fresh = page.read_only_snapshot()
        if (fresh.get("read_only") is not True or fresh.get("target_id") != target_id
                or fresh.get("url") != expected_url or not isinstance(fresh.get("html"), str)):
            raise LivePreparationError("target drift after form mutation")
        updated = prepare(html_text=fresh["html"], page_url=expected_url)
        if _identity(updated) != expected or updated.get("submission_enabled") is not False:
            raise LivePreparationError("application identity drift after form mutation")
        if updated.get("gates") or fresh.get("gates"):
            raise LivePreparationError("rendered application gate appeared after preparation")
        questions = updated.get("questions", [])
        if not isinstance(questions, list):
            raise LivePreparationError("invalid fresh question inventory")
        answer_coverage = coverage(profile=profile, questions=questions, company=identity["company"])
    answer_coverage = _resolve_verified_mapped_coverage(
        answer_coverage, applied_answers
    )
    from schonfeld_form import URL as SCHONFELD_URL
    if expected_url == SCHONFELD_URL:
        reader = getattr(page, "read_greenhouse_client_bound_form", None)
        if not callable(reader):
            raise LivePreparationError("complete learned client form inventory required")
        observed = reader()
        complete = observed.get("completeness", {})
        required = complete.get("required_selectors", [])
        applied_selectors = {item.get("selector") for item in applied_answers.get("field_evidence", []) if item.get("verified") is True}
        bindings = observed.get("bindings", {})
        if not (
            observed.get("source") == "live_client_bound_form" and observed.get("server_saved") is False
            and complete.get("verified") is True and required
            and all(complete.get(key) == [] for key in ("unknown_controls", "invalid_controls", "gates"))
            and set(required) <= applied_selectors
            and all(bindings.get(selector, {}).get("bound") is True and observed.get("fields", {}).get(selector)
                    for selector in required)
        ):
            raise LivePreparationError("complete approved client-bound form inventory required; unknowns fail closed")
        # Opaque ATS question labels are resolved by exact learned controls plus
        # actual full-form inventory, not by guessing answers from label text.
        answer_coverage = {"known": [{"question_key": key, "source": "verified_approved_answer"} for key in answers],
                           "human_required": [], "company_specific": [], "optional_skip": []}
    review_ready = not answer_coverage.get("human_required")
    return {
        "target_id": target_id,
        "page_url": expected_url,
        "identity": identity,
        "platform": prepared.get("platform", ""),
        "submission_enabled": False,
        "review_ready": review_ready,
        "answer_coverage": answer_coverage,
        "applied_answers": applied_answers,
        "evidence": {"sanitized": True, "target_bound": True},
    }


def _load_json_object(path: str, *, label: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise LivePreparationError(f"{label} must be a JSON object")
    return payload


def _validate_local_cdp_base_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise LivePreparationError("CDP base URL must be an uncredentialed loopback HTTP origin")


def _questions_from_fields(payload: dict) -> list[dict[str, object]]:
    fields = payload.get("fields", [])
    if not isinstance(fields, list):
        raise LivePreparationError("handler returned an invalid field inventory")
    questions: list[dict[str, object]] = []
    grouped_radio_names: set[str] = set()
    grouped_checkbox_names: set[str] = set()
    for field in fields:
        if not isinstance(field, dict):
            continue
        label = field.get("label") or field.get("name")
        if isinstance(label, str) and label:
            question = {"label": label, "required": field.get("required") is True}
            if field.get("type") in ("combobox", "checkbox"):
                question["type"] = field["type"]
            if field.get("type") == "checkbox":
                name = field.get("name")
                if isinstance(name, str) and name in grouped_checkbox_names:
                    continue
                groups = payload.get("choice_groups", [])
                matches = [group for group in groups if isinstance(group, dict)
                           and group.get("name") == name] if isinstance(groups, list) else []
                siblings = [other for other in fields if isinstance(other, dict) and other.get("name") == name]
                group = matches[0] if len(matches) == 1 else None
                options = group.get("options") if isinstance(group, dict) else None
                if (isinstance(name, str) and name and isinstance(group, dict)
                        and group.get("source") == "static_html" and group.get("type") == "checkbox"
                        and group.get("bound_values_verified") is False
                        and isinstance(group.get("label"), str) and group["label"].strip()
                        and isinstance(group.get("required"), bool)
                        and isinstance(options, list) and options and len(options) == len(siblings)
                        and all(isinstance(option, dict) and isinstance(option.get("label"), str)
                                and option["label"].strip() for option in options)
                        and all(other.get("type") == "checkbox" and isinstance(other.get("label"), str)
                                for other in siblings)
                        and sorted(option["label"] for option in options) == sorted(other["label"] for other in siblings)):
                    grouped_checkbox_names.add(name)
                    question["label"] = group["label"]
                    question["required"] = group["required"] or any(other.get("required") is True for other in siblings)
                    question["native_checkbox"] = group
                else:
                    question["native_checkbox"] = None
            if field.get("type") == "radio":
                name = field.get("name")
                groups = payload.get("choice_groups", [])
                matches = [group for group in groups if isinstance(group, dict)
                           and group.get("name") == name] if isinstance(groups, list) else []
                siblings = [other for other in fields if isinstance(other, dict) and other.get("name") == name]
                group = matches[0] if len(matches) == 1 else None
                if (isinstance(name, str) and name and isinstance(group, dict)
                        and group.get("source") == "static_html" and group.get("type") == "radio"
                        and group.get("label") == field.get("label")
                        and isinstance(group.get("options"), list) and len(group["options"]) == len(siblings)
                        and all(other.get("type") == "radio" and other.get("label") == field.get("label")
                                for other in siblings)):
                    if name in grouped_radio_names:
                        continue
                    grouped_radio_names.add(name)
                    question["required"] = any(other.get("required") is True for other in siblings)
                    question["native_radio"] = group
                else:
                    question["native_radio"] = None
            if field.get("type") == "select":
                groups = payload.get("select_groups", [])
                matches = [group for group in groups if isinstance(group, dict)
                           and field.get("name") and group.get("name") == field["name"]
                           and group.get("label") == field.get("label")] if isinstance(groups, list) else []
                same_name = sum(isinstance(other, dict) and other.get("type") == "select"
                                and other.get("name") == field.get("name") for other in fields)
                question["native_select"] = matches[0] if len(matches) == 1 and same_name == 1 else None
            questions.append(question)
    return questions


def _has_exact_identity(text: str, value: str) -> bool:
    if not value:
        return False
    return re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", text) is not None


def _dispatch_live_html(
    *,
    html_text: str,
    page_url: str,
    expected_identity: dict[str, str],
    expected_platform: str | None = None,
    official_posting: dict | None = None,
) -> dict:
    payload = prepare_saved_html(html_text=html_text, page_url=page_url)
    if expected_platform is not None and payload.get("platform") != expected_platform:
        raise LivePreparationError("learned field-map platform did not match ATS handler")
    manual_gates = payload.get("manual_gates") or (
        [payload["manual_gate"]] if payload.get("manual_gate") else []
    )
    if manual_gates:
        raise LivePreparationError("handler reported a human-required gate before preparation")
    if payload.get("page_type") not in {None, "application"}:
        raise LivePreparationError("handler did not report an application form surface")

    identity_sources = {
        "company": html_text,
        "role": html_text,
        "requisition": f"{html_text}\n{page_url}",
    }
    for key, expected in expected_identity.items():
        observed = payload.get(key)
        if not observed and _has_exact_identity(identity_sources[key], expected):
            payload[key] = expected
    if official_posting is not None:
        from schonfeld_form import URL
        if not (
            page_url == URL and payload.get("platform") == "greenhouse"
            and isinstance(official_posting, dict)
            and official_posting.get("id") == 8171772
            and official_posting.get("absolute_url") == page_url
            and official_posting.get("title") == expected_identity["role"]
            and str(official_posting.get("company_name", "")).strip() == expected_identity["company"]
            and official_posting.get("requisition_id") == expected_identity["requisition"]
            and all(_has_exact_identity(html_text, expected_identity[key]) for key in ("company", "role"))
        ):
            raise LivePreparationError("official posting identity drift")
        payload["requisition"] = official_posting["requisition_id"]
        payload["identity_source"] = "official_greenhouse_posting"
    payload["questions"] = _questions_from_fields(payload)
    return payload


def _apply_text_answers(
    *, page: object, target_id: str, expected_url: str, answers: dict[str, str]
) -> dict[str, object]:
    executor = CDPPageExecutor(page)  # type: ignore[arg-type]
    field_evidence = [
        executor.replace_text(
            target_id=target_id,
            expected_url=expected_url,
            selector=selector,
            value=value,
        )
        for selector, value in answers.items()
    ]
    return {
        "action": "fill_known_page",
        "field_evidence": field_evidence,
        "verified": bool(field_evidence) and all(item.get("verified") is True for item in field_evidence),
    }


def _sanitize_review_evidence(payload: dict) -> dict:
    applied = payload.get("applied_answers", {})
    sanitized_fields = []
    for field in applied.get("field_evidence", []):
        sanitized_fields.append({
            key: field[key]
            for key in ("action", "field", "selector", "verified", "target_id", "target_url")
            if key in field
        })
    return {
        **payload,
        "applied_answers": {
            "action": applied.get("action", "fill_known_page"),
            "field_evidence": sanitized_fields,
            "verified": applied.get("verified") is True,
        },
        "evidence": {
            **payload.get("evidence", {}),
            "answer_values_persisted": False,
        },
    }


def main(
    argv: list[str] | None = None,
    *,
    transport_factory: TransportFactory = ScopedCDPTransport,
    prepare: Prepare | None = None,
    coverage: Coverage = build_coverage_matrix,
) -> int:
    parser = argparse.ArgumentParser(description="Prepare one exact-bound live ATS page without submitting")
    parser.add_argument("--cdp-base-url", default="http://127.0.0.1:9222")
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--expected-url", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--requisition", required=True)
    parser.add_argument("--platform")
    parser.add_argument("--step")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--approved-answers", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    try:
        _validate_local_cdp_base_url(args.cdp_base_url)
        expected_identity = {
            "company": args.company,
            "role": args.role,
            "requisition": args.requisition,
        }
        if not args.target_id or not all(expected_identity.values()):
            raise LivePreparationError("target and expected identity values must be non-empty")
        profile = _load_json_object(args.profile, label="profile")
        approved_answers = _load_json_object(args.approved_answers, label="approved answers")
        if not approved_answers or not all(
            isinstance(field, str) and bool(field) for field in approved_answers
        ):
            raise LivePreparationError("approved answers must be a non-empty object")

        learned_actions = None
        if prepare is None:
            if not isinstance(args.platform, str) or not args.platform or not isinstance(args.step, str) or not args.step:
                raise LivePreparationError("default live preparation requires exact platform and learned step")
            mapping = resolve_field_map(page_url=args.expected_url, platform=args.platform)
            learned_actions = build_step_actions(
                mapping=mapping,
                step=args.step,
                approved_answers=approved_answers,
            )
            selected_prepare = lambda **kwargs: _dispatch_live_html(
                **kwargs,
                expected_identity=expected_identity,
                expected_platform=args.platform,
            )
        else:
            if not all(isinstance(value, str) for value in approved_answers.values()):
                raise LivePreparationError("injected preparation answers must be selector-to-string values")
            selected_prepare = prepare
        transport = transport_factory(args.cdp_base_url)
        with transport.bind_mutable_page_target(args.target_id) as page:  # type: ignore[attr-defined]
            if learned_actions is not None:
                apply_operation = lambda answers: execute_step_actions(
                    page=page,
                    target_id=args.target_id,
                    expected_url=args.expected_url,
                    actions=learned_actions,
                )
            else:
                apply_operation = lambda answers: _apply_text_answers(
                    page=page,
                    target_id=args.target_id,
                    expected_url=args.expected_url,
                    answers=answers,  # type: ignore[arg-type]
                )
            result = prepare_live_job(
                page=page,
                target_id=args.target_id,
                expected_url=args.expected_url,
                expected_identity=expected_identity,
                profile=profile,
                prepare=selected_prepare,
                coverage=coverage,
                approved_answers=approved_answers,
                apply_known=apply_operation,
            )
        sanitized = _sanitize_review_evidence(result)
        Path(args.output).write_text(json.dumps(sanitized, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "error": str(exc),
            "submission_enabled": False,
            "review_evidence_persisted": False,
        }))
        return 2

    print(json.dumps(sanitized, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
