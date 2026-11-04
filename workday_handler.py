#!/usr/bin/env python3
"""Fixture-driven Workday application and listing inspector."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from pipeline import validate_confirmation_evidence
from browser_actions import inventory_form_fields
from security_gates import detect_security_gate


class _WorkdayHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_h1 = False
        self.in_h2 = False
        self.in_title = False
        self.document_title = ""
        self.role = ""
        self.steps: list[str] = []
        self.fields: list[dict] = []
        self.current_label_text: list[str] | None = None
        self.current_label_closed = False
        self.uploaded_names: list[str] = []
        self.text_chunks: list[str] = []
        self.location = ""
        self.entrypoint: dict[str, str] = {}
        self.start_actions: dict[str, str] = {}
        self.parsed_values: list[dict[str, str]] = []
        self._automation_stack: list[dict[str, str | list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        automation_id = attr_map.get("data-automation-id") or ""
        self._automation_stack.append({"tag": tag, "id": automation_id, "texts": []})
        if "data-parsed-value" in attr_map and "data-source-value" in attr_map:
            self.parsed_values.append(
                {
                    "section": attr_map.get("data-section") or "",
                    "field": attr_map.get("data-field") or "",
                    "parsed_value": attr_map.get("data-parsed-value") or "",
                    "expected_value": attr_map.get("data-source-value") or "",
                }
            )
        if tag == "h1":
            self.in_h1 = True
        if tag == "h2":
            self.in_h2 = True
        if tag == "title":
            self.in_title = True
        action_names = {
            "autofillWithResume": "autofill_with_resume",
            "applyManually": "apply_manually",
            "useMyLastApplication": "use_last_application",
        }
        if automation_id in action_names:
            self.start_actions[action_names[automation_id]] = attr_map.get("href") or ""
        if tag == "label":
            self.current_label_text = []
            self.current_label_closed = False
        if tag in {"input", "select", "textarea"} and self.current_label_text is not None:
            self.current_label_closed = True
            field_type = attr_map.get("type") or ("textarea" if tag == "textarea" else tag)
            self.fields.append(
                {
                    "label": "",
                    "name": attr_map.get("name") or attr_map.get("id") or "",
                    "type": field_type,
                    "required": "required" in attr_map,
                }
            )
            uploaded = attr_map.get("data-uploaded-filename")
            if uploaded:
                self.uploaded_names.append(uploaded)

    def handle_endtag(self, tag: str) -> None:
        current = self._automation_stack.pop()
        automation_id = str(current.get("id") or "")
        text = " ".join(" ".join(current.get("texts") or []).split())
        if automation_id and text:
            if automation_id == "jobPostingHeader":
                self.role = text
            elif automation_id == "locations" and not self.location:
                self.location = text.removeprefix("locations ").strip()
            elif automation_id == "adventureButton":
                self.entrypoint["apply_label"] = text
            elif automation_id == "utilityButtonSignIn":
                self.entrypoint["sign_in_label"] = text
            elif automation_id == "requisitionId":
                self.entrypoint["requisition_id"] = text.removeprefix("job requisition id ").strip()
        if self._automation_stack and text:
            parent_texts = self._automation_stack[-1]["texts"]
            assert isinstance(parent_texts, list)
            parent_texts.append(text)
        if tag == "h1":
            self.in_h1 = False
        if tag == "h2":
            self.in_h2 = False
        if tag == "title":
            self.in_title = False
        if tag == "label" and self.current_label_text is not None:
            label = " ".join("".join(self.current_label_text).replace("*", " ").split())
            if label and self.fields:
                self.fields[-1]["label"] = label
            self.current_label_text = None
            self.current_label_closed = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        self.text_chunks.append(text)
        if self.in_h1 and not self.role:
            self.role = text
        if self.in_title and not self.document_title:
            self.document_title = text
        if self.in_h2:
            self.steps.append(text)
        if self.current_label_text is not None and not self.current_label_closed:
            self.current_label_text.append(text)
        if self._automation_stack:
            texts = self._automation_stack[-1]["texts"]
            assert isinstance(texts, list)
            texts.append(text)
        if text.casefold().endswith((".pdf", ".doc", ".docx")):
            self.uploaded_names.append(text)


def _manual_gates(text_chunks: list[str]) -> list[dict[str, str]]:
    text = " ".join(text_chunks).casefold()
    gates: list[dict[str, str]] = []
    security_gate = detect_security_gate(text_chunks)
    if security_gate and security_gate["type"] == "captcha":
        gates.append(security_gate)
    if "email verification" in text or "verify your email" in text:
        gates.append({"type": "email_verification", "detail": "Email verification required"})
    if "assessment" in text or "take-home" in text or "takehome" in text:
        gates.append({"type": "assessment", "detail": "Assessment detected"})
    if security_gate and security_gate["type"] != "captcha":
        gates.append(security_gate)
    return gates


def inspect_html(html_text: str, *, page_url: str, expected_resume_basename: str | None = None) -> dict:
    parser = _WorkdayHTMLParser()
    parser.feed(html_text)
    parser.fields = inventory_form_fields(html_text)
    confirmation_text = None
    page_type = "application"
    try:
        confirmation_text = validate_confirmation_evidence(
            confirmation_url=page_url,
            confirmation_text=html_text,
        )
        page_type = "confirmation"
    except ValueError:
        pass
    if page_type == "application" and not parser.fields and parser.entrypoint.get("apply_label"):
        page_type = "listing"
    if page_type == "application" and not parser.fields and parser.start_actions:
        page_type = "application_start"
    if page_type == "application" and not parser.fields:
        page_type = "unknown"
    role = parser.role
    if (not role or role.casefold().startswith("start your application")) and parser.document_title:
        role = parser.document_title
    uploaded_resume_verified = None
    if expected_resume_basename is not None:
        uploaded_resume_verified = expected_resume_basename in parser.uploaded_names
    manual_gates = _manual_gates(parser.text_chunks)
    reference_match = re.search(
        r"\breference\s+id\s*:\s*([A-Za-z0-9_-]+)",
        " ".join(parser.text_chunks),
        flags=re.IGNORECASE,
    )
    parse_issues = [
        item for item in parser.parsed_values
        if item["parsed_value"] != item["expected_value"]
    ]
    blocked = bool(manual_gates or parse_issues or uploaded_resume_verified is False)
    return {
        "page_type": page_type,
        "page_url": page_url,
        "tenant": urlparse(page_url).netloc,
        "role": role,
        "location": parser.location,
        "steps": parser.steps,
        "fields": parser.fields,
        "entrypoint": parser.entrypoint,
        "start_actions": parser.start_actions,
        "uploaded_resume_verified": uploaded_resume_verified,
        "manual_gate": manual_gates[0] if manual_gates else None,
        "manual_gates": manual_gates,
        "save_draft_available": any("save as draft" in text.casefold() for text in parser.text_chunks),
        "parse_issues": parse_issues,
        "safe_to_prepare": page_type == "application" and not blocked,
        "confirmation_text": confirmation_text,
        "confirmation_reference_id": reference_match.group(1) if reference_match else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("html_path")
    parser.add_argument("--page-url", required=True)
    parser.add_argument("--expected-resume-basename")
    args = parser.parse_args(argv)
    payload = inspect_html(
        Path(args.html_path).read_text(encoding="utf-8"),
        page_url=args.page_url,
        expected_resume_basename=args.expected_resume_basename,
    )
    print(json.dumps(payload))
    return 2 if payload["manual_gates"] or payload["parse_issues"] or payload["uploaded_resume_verified"] is False else 0


if __name__ == "__main__":
    raise SystemExit(main())
