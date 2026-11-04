#!/usr/bin/env python3
"""Fixture-driven Lever application inspector."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from pipeline import validate_confirmation_evidence
from browser_actions import _FormInventoryParser, inventory_form_fields
from security_gates import detect_security_gate


class _LeverHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_h1 = False
        self.current_label_for: str | None = None
        self.current_label_text: list[str] | None = None
        self.role = ""
        self.location = ""
        self.fields: list[dict] = []
        self.label_by_id: dict[str, str] = {}
        self.uploaded_names: list[str] = []
        self.text_chunks: list[str] = []
        self.gate_text_chunks: list[str] = []
        self._non_content_depth = 0
        self._location_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._non_content_depth += 1
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        if tag == "h1":
            self.in_h1 = True
        if tag == "label":
            self.current_label_for = attr_map.get("for")
            self.current_label_text = []
        if "sort-by-location" in classes:
            self._location_depth += 1
        if tag in {"input", "textarea", "select"}:
            field_id = attr_map.get("id") or ""
            field_type = attr_map.get("type") or ("textarea" if tag == "textarea" else tag)
            self.fields.append(
                {
                    "label": self.label_by_id.get(field_id, ""),
                    "name": attr_map.get("name") or field_id,
                    "type": field_type,
                    "required": "required" in attr_map,
                }
            )
            uploaded = attr_map.get("data-uploaded-filename")
            if uploaded:
                self.uploaded_names.append(uploaded)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._non_content_depth = max(0, self._non_content_depth - 1)
        if tag == "h1":
            self.in_h1 = False
        if tag == "label" and self.current_label_text is not None:
            label = " ".join("".join(self.current_label_text).split())
            if self.current_label_for:
                self.label_by_id[self.current_label_for] = label
                for field in self.fields:
                    if field["name"] == self.current_label_for:
                        field["label"] = label
            self.current_label_for = None
            self.current_label_text = None
        if self._location_depth and tag in {"span", "div"}:
            self._location_depth -= 1

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        self.text_chunks.append(text)
        if not self._non_content_depth:
            self.gate_text_chunks.append(text)
        if self.in_h1 and not self.role:
            self.role = text
        if self.current_label_text is not None:
            self.current_label_text.append(text)
        if self._location_depth and not self.location:
            self.location = text
        if text.lower().endswith((".pdf", ".doc", ".docx")):
            self.uploaded_names.append(text)


def _company_from_url(page_url: str) -> str:
    match = re.search(r"jobs\.lever\.co/([^/]+)", page_url)
    if match:
        token = match.group(1).replace("-", " ").replace("_", " ").strip()
        return " ".join(part.capitalize() for part in token.split())
    host = urlparse(page_url).netloc
    return host or "Unknown"


def _detect_manual_gate(text_chunks: list[str]) -> dict | None:
    return detect_security_gate(text_chunks)


def _card_heading(node: dict, nodes: list[dict]) -> str | None:
    """Explicit enclosing card metadata, kept separate from the field prompt."""
    cards = [index for index in node["ancestors"]
             if nodes[index]["attrs"].get("data-qa") == "additional-cards"]
    if not cards:
        return None
    headings = [candidate for candidate in nodes
                if candidate["tag"] == "h4" and candidate["attrs"].get("data-qa") == "card-name"
                and candidate["ancestors"] and candidate["ancestors"][-1] == cards[-1]]
    if len(headings) != 1:
        return None
    return " ".join("".join(headings[0]["text"]).split()) or None


def _paired_location_picker(node: dict, nodes: list[dict]) -> bool:
    """Recognize the explicit static Lever picker, never a selected value."""
    attrs = node["attrs"]
    if (node["tag"] != "input" or attrs.get("type") != "text"
            or attrs.get("name") != "location" or attrs.get("data-qa") != "location-input"
            or "location-input" not in (attrs.get("class") or "").split()
            or not node["ancestors"]):
        return False
    parent = node["ancestors"][-1]
    if "application-field" not in (nodes[parent]["attrs"].get("class") or "").split():
        return False
    siblings = [(i, other) for i, other in enumerate(nodes)
                if other["ancestors"] and other["ancestors"][-1] == parent]
    if sum(other["tag"] == "input" and other["attrs"].get("name") == "location"
           for _, other in siblings) != 1:
        return False
    hidden = [other for _, other in siblings if other["tag"] == "input"
              and other["attrs"].get("name") == "selectedLocation"
              and other["attrs"].get("type") == "hidden"]
    dropdowns = [i for i, other in siblings
                 if "dropdown-container" in (other["attrs"].get("class") or "").split()]
    return len(hidden) == 1 and len(dropdowns) == 1 and any(
        dropdowns[0] in other["ancestors"]
        and "dropdown-results" in (other["attrs"].get("class") or "").split()
        for other in nodes)


def _inventory_lever_fields(html_text: str) -> tuple[list[dict], list[dict]]:
    """Resolve the question heading, not a radio option's wrapping label."""
    parser = _FormInventoryParser()
    parser.feed(html_text)
    nodes = parser.nodes
    fields = inventory_form_fields(html_text)
    groups: dict[tuple[int, str, str], dict] = {}
    controls = [n for n in nodes if n["tag"] in {"input", "textarea", "select"}
                or n["attrs"].get("role") == "combobox"]
    for node, field in zip(controls, fields):
        if _paired_location_picker(node, nodes):
            field["type"] = "combobox"
        questions = [i for i in node["ancestors"] if "application-question" in
                     (nodes[i]["attrs"].get("class") or "").split()]
        if not questions:
            continue
        question = questions[-1]
        labels = [n for n in nodes if question in n["ancestors"]
                  and "application-label" in (n["attrs"].get("class") or "").split()]
        if len(labels) == 1:
            option_label = field["label"]
            field["label"] = " ".join(" ".join(labels[0]["text"]).split()).rstrip("*✱ ")
            heading = _card_heading(node, nodes)
            if heading:
                field["card_heading"] = heading
            name = node["attrs"].get("name")
            if field["type"] in {"radio", "checkbox"} and name:
                group = groups.setdefault((question, name, field["type"]), {
                    "name": name, "label": field["label"], "type": field["type"],
                    "required": False, "options": [], "source": "static_html",
                    "bound_values_verified": False,
                })
                group["required"] = group["required"] or field["required"]
                if heading:
                    group["card_heading"] = heading
                option = {"label": option_label, "value": node["attrs"].get("value")}
                if field["type"] == "radio":
                    option["disabled"] = "disabled" in node["attrs"] or any(
                        nodes[i]["tag"] == "fieldset" and "disabled" in nodes[i]["attrs"]
                        for i in node["ancestors"])
                group["options"].append(option)
    return fields, list(groups.values())


class _NativeSelectTree(_FormInventoryParser):
    """Only native option end-tag rules; does not change shared form parsing."""
    def handle_starttag(self, tag, attrs):
        if tag in {"option", "optgroup"}:
            self.handle_endtag("option")
        if tag == "optgroup":
            self.handle_endtag("optgroup")
        super().handle_starttag(tag, attrs)


def _native_select_groups(html_text: str, fields: list[dict]) -> list[dict]:
    """Static native select definitions; do not infer selected/saved values."""
    parser = _NativeSelectTree()
    parser.feed(html_text)
    nodes = parser.nodes
    controls = [(i, node) for i, node in enumerate(nodes)
                if node["tag"] in {"input", "textarea", "select"}
                or node["attrs"].get("role") == "combobox"]
    groups = []
    for (index, node), field in zip(controls, fields):
        if node["tag"] != "select":
            continue
        options = [{"label": option["attrs"].get("label") or " ".join("".join(option["text"]).split()),
                    "value": option["attrs"].get("value"),
                    "disabled": "disabled" in option["attrs"] or any(
                        nodes[i]["tag"] == "optgroup" and "disabled" in nodes[i]["attrs"]
                        for i in option["ancestors"])}
                   for option in nodes if option["tag"] == "option" and index in option["ancestors"]]
        groups.append({"id": node["attrs"].get("id"), "name": field["name"],
                       "label": field["label"], "type": "select", "required": field["required"],
                       "multiple": "multiple" in node["attrs"], "disabled": "disabled" in node["attrs"],
                       "options": options, "source": "static_html", "bound_values_verified": False})
    return groups


def _posting_header_identity(html_text: str) -> dict[str, str]:
    """Read unique values from one explicit header, never arbitrary body headings."""
    parser = _FormInventoryParser()
    parser.feed(html_text)
    headers = [i for i, node in enumerate(parser.nodes)
               if "posting-header" in (node["attrs"].get("class") or "").split()]
    if len(headers) != 1:
        return {}
    descendants = [node for node in parser.nodes if headers[0] in node["ancestors"]]
    candidates = {
        "role": [node for node in descendants if node["tag"] == "h2"],
        "location": [node for node in descendants if {"posting-category", "location"}
                     <= set((node["attrs"].get("class") or "").split())],
    }
    return {key: " ".join("".join(nodes[0]["text"]).split())
            for key, nodes in candidates.items() if len(nodes) == 1}


def inspect_html(html_text: str, *, page_url: str, expected_resume_basename: str | None = None) -> dict:
    parser = _LeverHTMLParser()
    parser.feed(html_text)
    identity = _posting_header_identity(html_text)
    parser.fields, choice_groups = _inventory_lever_fields(html_text)
    uploaded_resume_verified = None
    if expected_resume_basename is not None:
        uploaded_resume_verified = expected_resume_basename in parser.uploaded_names
    manual_gate = _detect_manual_gate(parser.gate_text_chunks)
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

    return {
        "page_type": page_type,
        "company": _company_from_url(page_url),
        "role": identity.get("role") or parser.role,
        "location": identity.get("location") or parser.location,
        "fields": parser.fields,
        "choice_groups": choice_groups,
        "select_groups": _native_select_groups(html_text, parser.fields),
        "uploaded_resume_verified": uploaded_resume_verified,
        "manual_gate": manual_gate,
        "confirmation_text": confirmation_text,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
