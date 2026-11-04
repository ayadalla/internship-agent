#!/usr/bin/env python3
"""Fixture-driven CGI/Njoyn application inspector."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from pipeline import validate_confirmation_evidence
from browser_actions import inventory_form_fields


class _NjoynHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.surface = ""
        self.in_h1 = False
        self.role = ""
        self.company = ""
        self.location = ""
        self.entrypoint: dict[str, str] = {}
        self.fields: list[dict[str, str]] = []
        self.uploaded_names: list[str] = []
        self.parser_mismatches: list[str] = []
        self.selected_options: dict[str, str] = {}
        self._apply_link_text: list[str] | None = None
        self._label_text: list[str] | None = None
        self._label_field: dict[str, str] | None = None
        self._button_text: list[str] | None = None
        self._active_select_name: str | None = None
        self._option_text: list[str] | None = None
        self._option_selected = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        uploaded_name = attr_map.get("data-uploaded-file")
        if uploaded_name:
            self.uploaded_names.append(uploaded_name)
        parser_mismatch = attr_map.get("data-parser-mismatch")
        if parser_mismatch:
            self.parser_mismatches.append(parser_mismatch)
        if tag == "main":
            self.surface = attr_map.get("data-surface") or self.surface
        if tag == "h1":
            self.in_h1 = True
        if tag == "label":
            self._label_text = []
            self._label_field = None
        if tag == "input":
            label = " ".join("".join(self._label_text or []).split())
            field = {
                "name": attr_map.get("name") or "",
                "type": attr_map.get("type") or "text",
                "label": label,
            }
            self.fields.append(field)
            if self._label_text is not None:
                self._label_field = field
        if tag == "select":
            self._active_select_name = attr_map.get("name") or ""
            self.fields.append(
                {
                    "name": attr_map.get("name") or "",
                    "type": "select",
                    "label": self.role,
                }
            )
        if tag == "option":
            self._option_text = []
            self._option_selected = "selected" in attr_map
        if tag == "button":
            self._button_text = []
        href = attr_map.get("href") or ""
        if tag == "a" and "apply" in " ".join(
            (href, attr_map.get("class") or "")
        ).casefold():
            self._apply_link_text = []
            if href:
                self.entrypoint["apply_url"] = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self.in_h1 = False
        if tag == "label":
            if self._label_field is not None:
                self._label_field["label"] = " ".join(
                    "".join(self._label_text or []).split()
                )
            self._label_text = None
            self._label_field = None
        if tag == "button" and self._button_text is not None:
            label = " ".join("".join(self._button_text).split())
            if label.casefold() == "create a profile":
                self.entrypoint["create_profile_label"] = label
            self._button_text = None
        if tag == "option" and self._option_text is not None:
            if self._option_selected and self._active_select_name:
                self.selected_options[self._active_select_name] = " ".join(
                    "".join(self._option_text).split()
                )
            self._option_text = None
            self._option_selected = False
        if tag == "select":
            self._active_select_name = None
        if tag == "a" and self._apply_link_text is not None:
            label = " ".join("".join(self._apply_link_text).split())
            if label:
                self.entrypoint["apply_label"] = label
            self._apply_link_text = None

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self.in_h1 and not self.role:
            self.role = text
        if self._apply_link_text is not None:
            self._apply_link_text.append(text)
        if self._label_text is not None:
            self._label_text.append(text)
        if self._button_text is not None:
            self._button_text.append(text)
        if self._option_text is not None:
            self._option_text.append(text)
        if not self.company and "·" in text:
            company, _, location = text.partition("·")
            self.company = company.strip()
            self.location = location.strip()


def inspect_html(html_text: str, *, page_url: str, expected_resume_basename: str | None = None) -> dict:
    """Inventory a sanitized Njoyn surface without performing mutations."""
    parser = _NjoynHTMLParser()
    parser.feed(html_text)
    parser.fields = inventory_form_fields(html_text)
    confirmation_text = None
    confirmation_reference_id = None
    page_type = "application"
    try:
        confirmation_text = validate_confirmation_evidence(
            confirmation_url=page_url,
            confirmation_text=html_text,
        )
        reference_match = re.search(
            r"\bReference:\s*([A-Za-z0-9-]+)", confirmation_text
        )
        if reference_match:
            confirmation_reference_id = reference_match.group(1)
        page_type = "confirmation"
    except ValueError:
        pass
    if page_type == "application" and parser.surface == "listing" and parser.entrypoint:
        page_type = "listing"
    if page_type == "application" and parser.surface == "account":
        page_type = "account"
    if page_type == "application" and parser.surface == "privacy":
        page_type = "privacy"
    if page_type == "application" and parser.surface == "disclosures":
        page_type = "disclosures"
    if page_type == "application" and parser.surface == "disability":
        page_type = "disability"
    if page_type == "application" and parser.surface == "resume-upload":
        page_type = "resume_upload"
    if page_type == "application" and parser.surface == "parsed-profile":
        page_type = "parsed_profile"
    if page_type == "application" and parser.surface == "referral":
        page_type = "referral"
    if page_type == "application" and parser.surface == "questionnaire":
        page_type = "questionnaire"
    uploaded_resume_verified = None
    if expected_resume_basename is not None:
        uploaded_resume_verified = expected_resume_basename in parser.uploaded_names
    manual_gate = None
    if page_type == "account":
        manual_gate = {
            "type": "account_sign_in",
            "detail": "Sign in or create a profile required",
        }
    if page_type == "privacy":
        manual_gate = {
            "type": "privacy_notice",
            "detail": "Privacy notice acknowledgement required",
        }
    if page_type == "disclosures":
        manual_gate = {
            "type": "employment_disclosures",
            "detail": "Required employment disclosures must be answered",
        }
    if page_type == "disability":
        manual_gate = {
            "type": "disability_disclosure",
            "detail": "Voluntary disability disclosure must be explicitly handled",
        }
    if page_type == "parsed_profile" and parser.parser_mismatches:
        manual_gate = {
            "type": "parser_correction",
            "detail": "Parsed profile contains explicit mismatches that require correction",
        }
    referral_selection: dict[str, str | bool | None] = {
        "parent": parser.selected_options.get("source"),
        "child": parser.selected_options.get("source_detail"),
    }
    referral_selection["verified"] = referral_selection == {
        "parent": "Social Media",
        "child": "Instagram",
    }
    if page_type == "referral" and not referral_selection["verified"]:
        manual_gate = {
            "type": "referral_selection",
            "detail": "Social Media and Instagram must be selected as real options",
        }
    if page_type == "questionnaire":
        manual_gate = {
            "type": "unknown_required_questions",
            "detail": "Required questionnaire answers must be resolved before preparation",
        }
    return {
        "page_type": page_type,
        "surface": parser.surface,
        "page_url": page_url,
        "role": parser.role,
        "company": parser.company,
        "location": parser.location,
        "fields": parser.fields,
        "entrypoint": parser.entrypoint,
        "uploaded_resume_verified": uploaded_resume_verified,
        "parser_correction_required": bool(parser.parser_mismatches),
        "parser_mismatches": parser.parser_mismatches,
        **({"referral_selection": referral_selection} if page_type == "referral" else {}),
        "manual_gate": manual_gate,
        "confirmation_text": confirmation_text,
        "confirmation_reference_id": confirmation_reference_id,
        "safe_to_prepare": False,
    }


def main(argv: list[str] | None = None) -> int:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("html_path")
    argument_parser.add_argument("--page-url", required=True)
    argument_parser.add_argument("--expected-resume-basename")
    args = argument_parser.parse_args(argv)
    payload = inspect_html(
        Path(args.html_path).read_text(encoding="utf-8"),
        page_url=args.page_url,
        expected_resume_basename=args.expected_resume_basename,
    )
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
