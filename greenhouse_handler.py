#!/usr/bin/env python3
"""Fixture-driven Greenhouse application inspector."""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from pipeline import validate_confirmation_evidence
from browser_actions import _FormInventoryParser, inventory_form_fields
from security_gates import detect_security_gate


class _GreenhouseHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_h1 = False
        self.in_title = False
        self.current_field: dict | None = None
        self.current_label_text: list[str] | None = None
        self.current_label_for: str | None = None
        self.current_label_closed = False
        self.label_by_id: dict[str, str] = {}
        self.role = ""
        self.document_title = ""
        self.company = ""
        self.location = ""
        self.fields: list[dict] = []
        self.uploaded_names: list[str] = []
        self.text_chunks: list[str] = []
        self.entrypoint: dict[str, str] = {}
        self.controls: list[dict[str, str]] = []
        self._apply_link_text: list[str] | None = None
        self._ignored_text_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._ignored_text_depth += 1
        if tag == "h1":
            self.in_h1 = True
        if tag == "title":
            self.in_title = True
        if tag == "label":
            self.current_label_text = []
            self.current_label_for = attr_map.get("for")
            self.current_field = None
            self.current_label_closed = False
        if tag == "a" and (
            "apply" in (attr_map.get("class") or "").casefold()
            or (attr_map.get("href") or "").startswith("#app")
        ):
            self._apply_link_text = []
        if tag in {"input", "select", "textarea"}:
            if self.current_label_text is not None:
                self.current_label_closed = True
            field_type = attr_map.get("type") or (
                "textarea" if tag == "textarea" else "select" if tag == "select" else "text"
            )
            self.current_field = {
                "label": self.label_by_id.get(attr_map.get("id") or "", ""),
                "name": attr_map.get("name") or attr_map.get("id") or "",
                "type": field_type,
                "required": "required" in attr_map,
            }
            self.fields.append(self.current_field)
            self.controls.append({
                "id": attr_map.get("id") or "",
                "type": field_type,
                "value": attr_map.get("value") or "",
            })
            uploaded = attr_map.get("data-uploaded-filename")
            if uploaded:
                self.uploaded_names.append(uploaded)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_text_depth:
            self._ignored_text_depth -= 1
        if tag == "h1":
            self.in_h1 = False
        if tag == "title":
            self.in_title = False
        if tag == "label" and self.current_label_text is not None:
            label = " ".join("".join(self.current_label_text).replace("*", " ").split())
            if self.current_label_for:
                self.label_by_id[self.current_label_for] = label
                for field in self.fields:
                    if field["name"] == self.current_label_for:
                        field["label"] = label
            if self.current_field is not None:
                self.current_field["label"] = label
            self.current_label_text = None
            self.current_label_for = None
            self.current_field = None
        if tag == "a" and self._apply_link_text is not None:
            label = " ".join(self._apply_link_text)
            if label:
                self.entrypoint["apply_label"] = label
            self._apply_link_text = None

    def handle_data(self, data: str) -> None:
        if self._ignored_text_depth:
            return
        text = data.strip()
        if not text:
            return
        self.text_chunks.append(text)
        if self.in_h1 and not self.role:
            self.role = text
        if self.in_title:
            self.document_title += (" " if self.document_title else "") + text
        if self.current_label_text is not None and not self.current_label_closed:
            self.current_label_text.append(text)
        if self._apply_link_text is not None:
            self._apply_link_text.append(text)
        if not self.company and "—" in text:
            company, _, location = text.partition("—")
            self.company = company.strip()
            self.location = location.strip()
        if text.lower().endswith((".pdf", ".doc", ".docx")):
            self.uploaded_names.append(text)


def _detect_manual_gate(text_chunks: list[str]) -> dict | None:
    security_gate = detect_security_gate(text_chunks)
    if security_gate is not None:
        return security_gate
    lowered = " ".join(text_chunks).casefold()
    if (
        "verify your email" in lowered
        or "verify email address" in lowered
        or "email verification" in lowered
        or "confirm your email address" in lowered
    ):
        return {"type": "email_verification", "detail": "Email verification detected"}
    if "assessment" in lowered or "skills test" in lowered or "online evaluation" in lowered or "coding challenge" in lowered:
        return {"type": "assessment", "detail": "Assessment detected"}
    return None


def _build_control_hints(controls: list[dict[str, str]]) -> dict[str, object]:
    prefilled_ids = sorted(
        control["id"]
        for control in controls
        if control.get("id")
        and control.get("value")
        and control.get("type") in {"text", "textarea", "email", "tel", "number", "url", "search"}
    )
    field_names = {
        "school": "school",
        "degree": "degree",
        "discipline": "discipline",
        "start-month": "start_month",
        "start-year": "start_year",
        "end-month": "end_month",
        "end-year": "end_year",
    }
    grouped: dict[int, dict[str, str]] = {}
    for control in controls:
        control_id = control.get("id", "")
        match = re.fullmatch(
            r"(school|degree|discipline|start-month|start-year|end-month|end-year)--(\d+)",
            control_id,
        )
        if not match:
            continue
        field, raw_index = match.groups()
        grouped.setdefault(int(raw_index), {})[field_names[field]] = f"#{control_id}"
    return {
        "prefilled_ids": prefilled_ids,
        "education_groups": [
            {"index": index, "selectors": selectors}
            for index, selectors in sorted(grouped.items())
        ],
    }


def _fieldset_choice_groups(html_text: str, fields: list[dict]) -> list[dict]:
    """Pair explicit fieldset legends with static options, not selected state."""
    parser = _FormInventoryParser()
    parser.feed(html_text)
    nodes = parser.nodes
    controls = [node for node in nodes if node["tag"] in {"input", "textarea", "select"}
                or node["attrs"].get("role") == "combobox"]
    groups: dict[tuple[int, str], dict] = {}
    for node, field in zip(controls, fields):
        name = node["attrs"].get("name")
        if field["type"] != "checkbox" or not name:
            continue
        fieldset = next((i for i in reversed(node["ancestors"])
                         if nodes[i]["tag"] == "fieldset"), None)
        if fieldset is None:
            continue
        legends = [item for item in nodes if item["tag"] == "legend" and item["parent"] == fieldset]
        if len(legends) != 1:
            continue
        label = " ".join(" ".join(legends[0]["text"]).split()).rstrip("*✱ ")
        if not label:
            continue
        group = groups.setdefault((fieldset, name), {
            "fieldset_id": nodes[fieldset]["attrs"].get("id"), "name": name,
            "label": label, "type": "checkbox",
            "required": str(nodes[fieldset]["attrs"].get("aria-required", "")).lower() == "true",
            "options": [],
            "source": "static_html", "bound_values_verified": False,
        })
        group["required"] = group["required"] or field["required"]
        group["options"].append({"id": node["attrs"].get("id"),
                                 "label": field["label"], "value": node["attrs"].get("value")})
    return list(groups.values())


def _apply_upload_group_labels(html_text: str, fields: list[dict]) -> None:
    """Recover a unique upload question, not attachment state or upload authority."""
    parser = _FormInventoryParser()
    parser.feed(html_text)
    nodes = parser.nodes
    controls = [node for node in nodes if node["tag"] in {"input", "textarea", "select"}
                or node["attrs"].get("role") == "combobox"]
    for node, field in zip(controls, fields):
        control_id = node["attrs"].get("id")
        if field["type"] != "file" or not control_id:
            continue
        if sum(item["attrs"].get("id") == control_id for item in nodes) != 1:
            continue
        group = next((i for i in reversed(node["ancestors"])
                      if "file-upload" in (nodes[i]["attrs"].get("class") or "").split()), None)
        if group is None or nodes[group]["attrs"].get("role") != "group":
            continue
        refs = (nodes[group]["attrs"].get("aria-labelledby") or "").split()
        labels = [item for item in nodes if len(refs) == 1 and item["attrs"].get("id") == refs[0]]
        uploads = [item for item in controls if item["attrs"].get("type") == "file"
                   and group in item["ancestors"]]
        if (len(labels) != 1 or len(uploads) != 1 or group not in labels[0]["ancestors"]
                or "upload-label" not in (labels[0]["attrs"].get("class") or "").split()):
            continue
        label = " ".join("".join(labels[0]["text"]).split()).rstrip("*✱ ")
        if not label:
            continue
        if field["label"] in {"", "Attach"}:
            field["label"] = label
        field["required"] = field["required"] or nodes[group]["attrs"].get("aria-required") == "true"


def _react_multiselect_hints(html_text: str, fields: list[dict]) -> list[dict]:
    """Positive static multi-container evidence, never options or selected state."""
    parser = _FormInventoryParser()
    parser.feed(html_text)
    controls = [node for node in parser.nodes if node["tag"] in {"input", "textarea", "select"}
                or node["attrs"].get("role") == "combobox"]
    hints = []
    for node, field in zip(controls, fields):
        if node["attrs"].get("role") != "combobox" or not node["attrs"].get("id"):
            continue
        control_id = node["attrs"]["id"]
        if sum(item["attrs"].get("id") == control_id for item in parser.nodes) != 1:
            continue
        if not any("select__value-container--is-multi" in
                   (parser.nodes[i]["attrs"].get("class") or "").split()
                   for i in node["ancestors"]):
            continue
        hints.append({"id": node["attrs"]["id"], "label": field["label"],
                      "required": field["required"], "source": "static_html",
                      "bound_values_verified": False, "options_verified": False})
    return hints


def _modern_job_location(html_text: str) -> str | None:
    """Static scoped value; None means no modern header, empty means unknown."""
    tree = _FormInventoryParser()
    tree.feed(html_text)
    headers = [i for i, node in enumerate(tree.nodes)
               if "job__header" in (node["attrs"].get("class") or "").split()]
    if not headers:
        return None
    if len(headers) != 1:
        return ""
    locations = [i for i, node in enumerate(tree.nodes)
                 if headers[0] in node["ancestors"]
                 and "job__location" in (node["attrs"].get("class") or "").split()]
    if len(locations) != 1:
        return ""
    values = [node for node in tree.nodes
              if node["parent"] == locations[0] and node["tag"] == "div"]
    return " ".join("".join(values[0]["text"]).split()) if len(values) == 1 else ""


def _has_only_site_search(html_text: str) -> bool:
    """Recognize explicit GET /search forms without guessing rendered coverage."""
    parser = _FormInventoryParser()
    parser.feed(html_text)
    controls = [node for node in parser.nodes
                if node["tag"] in {"input", "textarea", "select"}
                or node["attrs"].get("role") == "combobox"]
    if not controls:
        return False
    for node in controls:
        if node["tag"] != "input" or str(node["attrs"].get("type", "")).lower() != "search":
            return False
        form = next((parser.nodes[i] for i in reversed(node["ancestors"])
                     if parser.nodes[i]["tag"] == "form"), None)
        if form is None or form["attrs"].get("action") != "/search":
            return False
        if str(form["attrs"].get("method", "get")).lower() != "get":
            return False
    return True


def inspect_html(html_text: str, *, page_url: str, expected_resume_basename: str | None = None) -> dict:
    parser = _GreenhouseHTMLParser()
    parser.feed(html_text)
    parser.fields = inventory_form_fields(html_text)
    _apply_upload_group_labels(html_text, parser.fields)
    title_match = re.fullmatch(
        r"Job Application for .+? at (.+)", parser.document_title.strip()
    )
    if title_match:
        parser.company = title_match.group(1).strip()
    uploaded_resume_verified = None
    if expected_resume_basename is not None:
        uploaded_resume_verified = expected_resume_basename in parser.uploaded_names
    manual_gate = _detect_manual_gate(parser.text_chunks)
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
    search_only = _has_only_site_search(html_text)
    modern_location = _modern_job_location(html_text)
    location = parser.location if modern_location is None else modern_location
    if page_type == "application" and search_only:
        page_type = "listing"
    control_hints = _build_control_hints(parser.controls)
    multiselects = _react_multiselect_hints(html_text, parser.fields)
    if multiselects:
        control_hints["react_multiselects"] = multiselects
    return {
        "page_type": page_type,
        "page_url": page_url,
        "company": parser.company if not search_only or title_match else "",
        "role": parser.role,
        "location": location if not search_only else "",
        "fields": parser.fields,
        "choice_groups": _fieldset_choice_groups(html_text, parser.fields),
        "entrypoint": parser.entrypoint,
        "control_hints": control_hints,
        **({"form_evidence": {"source": "static_html", "status": "search_only",
                              "rendering_verified": False}} if search_only else {}),
        "uploaded_resume_verified": uploaded_resume_verified,
        "manual_gate": manual_gate,
        "safe_to_prepare": page_type == "application" and manual_gate is None and uploaded_resume_verified is not False,
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
    return 2 if not payload["safe_to_prepare"] and payload["page_type"] == "application" else 0


if __name__ == "__main__":
    raise SystemExit(main())
