"""Read-only inspector for an application form open in the agent's Chrome.

    python form_inspector.py --list
    python form_inspector.py --url-contains greenhouse.io

It attaches over loopback CDP, runs ONE fixed DOM-reading script, and prints the
structure of the form: every control's label, id, type, required flag, native
options, group, visibility. It never types, clicks, focuses, scrolls, navigates,
uploads, or reads what is typed into a field, so nothing personal ends up in its
output. What it cannot know is reported as unknown, never guessed: for example the
options of a React-style dropdown only exist after it is opened, so they are
reported as ``options: null, options_verified: false``.

Read-only is enforced by code, not convention: the only connection handed to the
page is ReadOnlyConnection, which refuses every CDP method except Runtime.evaluate
and every expression except the two fixed constants in this file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from scoped_cdp import (
    CDPConnection,
    ScopedCDPTransport,
    TargetBindingError,
    WebSocketCDPConnection,
    fetch_json,
)

DEFAULT_BASE_URL = "http://127.0.0.1:9222"
MAX_CONTROLS = 500

LOCATION_EXPRESSION = "location.href"

# One fixed script. No interpolation, no events, no focus/scroll/click, and it
# never reads `.value` of text-like inputs or `.files`. For radio/checkbox it
# reads the `value` ATTRIBUTE (the option's identity, not user input) and the
# checked state.
INSPECT_EXPRESSION = r"""
JSON.stringify((() => {
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const textOfIds = (ids) => (ids || '').split(/\s+/).filter(Boolean)
    .map((id) => { const n = document.getElementById(id); return n ? clean(n.textContent) : ''; })
    .filter(Boolean).join(' ');
  const labelOf = (el) => {
    let t = textOfIds(el.getAttribute('aria-labelledby'));
    if (!t && el.labels && el.labels.length) t = clean([...el.labels].map((l) => l.textContent).join(' '));
    if (!t) t = clean(el.getAttribute('aria-label'));
    if (!t) { const w = el.closest('label'); if (w) t = clean(w.textContent); }
    return t;
  };
  const groupOf = (el) => {
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg) return clean(lg.textContent); }
    const g = el.closest('[role=group],[role=radiogroup]');
    if (g) return textOfIds(g.getAttribute('aria-labelledby')) || clean(g.getAttribute('aria-label'));
    return '';
  };
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0
      && el.getClientRects().length > 0;
  };
  const seen = new Set();
  const controls = [];
  const nodes = document.querySelectorAll(
    'input, select, textarea, button, [role=combobox], [role=radio], [role=checkbox], [contenteditable=true]');
  for (const el of nodes) {
    if (seen.has(el) || controls.length >= %(max)d) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const row = {
      tag, type, role,
      id: el.id || '', name: el.getAttribute('name') || '',
      label: labelOf(el), group: groupOf(el),
      required: Boolean(el.required) || el.getAttribute('aria-required') === 'true'
        || (type === 'radio' && el.name !== '' && [...document.getElementsByName(el.name)].some((r) => r.required)),
      disabled: Boolean(el.disabled), read_only: Boolean(el.readOnly),
      visible: type === 'hidden' ? false : visible(el),
      multiple: Boolean(el.multiple),
      accept: el.getAttribute('accept') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      has_popup: el.getAttribute('aria-haspopup') || '',
      max_length: el.getAttribute('maxlength') || '',
    };
    if (tag === 'select') {
      row.options = [...el.options].slice(0, 200).map((o) => ({
        value: o.getAttribute('value') || '', text: clean(o.textContent), disabled: Boolean(o.disabled)}));
      row.options_verified = true;
    } else if (role === 'combobox' || row.has_popup === 'listbox') {
      row.options = null; row.options_verified = false;
    }
    if (tag === 'button') row.text = clean(el.textContent);
    if (tag === 'input' && (type === 'submit' || type === 'button')) row.text = clean(el.getAttribute('value'));
    if (type === 'radio' || type === 'checkbox') {
      row.option_value = el.getAttribute('value') || '';
      row.checked = Boolean(el.checked);
    }
    controls.push(row);
  }
  return {title: clean(document.title), ready_state: document.readyState,
          truncated: nodes.length > controls.length, controls};
})())
""" % {"max": MAX_CONTROLS}

ALLOWED_METHODS = frozenset({"Runtime.evaluate"})
ALLOWED_EXPRESSIONS = frozenset({LOCATION_EXPRESSION, INSPECT_EXPRESSION})


class InspectorError(RuntimeError):
    """The inspector refused to act or could not trust what it saw."""


class ReadOnlyConnection:
    """Wraps a CDP connection so only the two fixed read-only evaluations can run."""

    def __init__(self, connection: CDPConnection) -> None:
        self._connection = connection

    def call(self, method: str, params: dict) -> dict:
        if method not in ALLOWED_METHODS:
            raise InspectorError(f"CDP method not allowed in read-only mode: {method}")
        if set(params) != {"expression", "returnByValue"} or params.get("returnByValue") is not True:
            raise InspectorError("unexpected evaluation parameters")
        if params["expression"] not in ALLOWED_EXPRESSIONS:
            raise InspectorError("only the fixed inspection expressions may be evaluated")
        return self._connection.call(method, params)

    def close(self) -> None:
        self._connection.close()


def validate_loopback_base_url(base_url: str) -> str:
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
        raise InspectorError("CDP base URL must be an uncredentialed loopback http origin")
    return base_url.rstrip("/")


def public_url(url: str) -> str:
    """scheme://host/path only: query strings and fragments can carry tokens."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def list_pages(base_url: str, *, fetch=fetch_json) -> list[dict]:
    base_url = validate_loopback_base_url(base_url)
    targets = fetch(f"{base_url}/json/list")
    if not isinstance(targets, list):
        raise InspectorError("CDP target list was not an array")
    pages = []
    for item in targets:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        url = item.get("url")
        if isinstance(url, str) and urlsplit(url).scheme in {"http", "https"}:
            pages.append({"id": item.get("id"), "title": item.get("title", ""), "url": public_url(url)})
    return pages


def choose_page(pages: list[dict], url_contains: str | None) -> dict:
    matches = [p for p in pages if not url_contains or url_contains in p["url"]]
    if not matches:
        raise InspectorError("no open page matches; run with --list to see what is open")
    if len(matches) > 1:
        raise InspectorError("more than one page matches; narrow --url-contains (see --list)")
    return matches[0]


_CORE_IDS = {"first_name", "last_name", "email", "phone", "resume", "cover_letter",
             "country", "candidate-location"}
_EDUCATION = re.compile(r"^(school|degree|discipline|start-month|start-year|end-month|end-year)--\d+$")
_EEO_LABEL = re.compile(r"\b(gender|race|ethnic\w*|hispanic|latino|veteran|disabilit\w*|sexual orientation|transgender)\b", re.I)
_CONSENT_LABEL = re.compile(r"\b(privacy|consent|agree|acknowledge|sms|text message)\b", re.I)


def categorize(control: dict) -> str:
    """Conservative bucket for a control; anything unclear is 'other', never guessed."""
    cid, label = control.get("id", ""), control.get("label", "")
    if control.get("tag") == "button" or control.get("type") in {"submit", "button"}:
        return "button"
    if control.get("type") == "hidden":
        return "hidden"
    if cid in _CORE_IDS or _EDUCATION.match(cid):
        return "standard"
    if _EEO_LABEL.search(label) or _EEO_LABEL.search(control.get("group", "")):
        return "eeo_demographic"
    if cid.startswith("question_") and control.get("type") == "checkbox" and _CONSENT_LABEL.search(
            label + " " + control.get("group", "")):
        return "consent"
    if cid.startswith("question_"):
        return "custom_question"
    return "other"


def build_report(payload: dict, *, page_url: str, inspected_at: str) -> dict:
    controls = payload.get("controls")
    if not isinstance(controls, list):
        raise InspectorError("inspection returned no control list")
    rows = []
    for control in controls:
        row = dict(control)
        row["category"] = categorize(control)
        rows.append(row)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return {
        "read_only": True,
        "values_recorded": False,
        "inspected_at": inspected_at,
        "page_url": public_url(page_url),
        "title": payload.get("title", ""),
        "ready_state": payload.get("ready_state", ""),
        "truncated": bool(payload.get("truncated")),
        "summary": {
            "controls": len(rows),
            "required": sum(1 for r in rows if r.get("required")),
            "unlabeled_visible": sum(
                1 for r in rows
                if r.get("visible") and not r.get("label") and not r.get("group")
                and r["category"] not in {"button", "hidden"}),
            "options_unverified": sum(1 for r in rows if r.get("options_verified") is False),
            "by_category": counts,
        },
        "controls": rows,
    }


def inspect_page(base_url: str, page: dict, *, fetch=fetch_json, connect=None) -> dict:
    base_url = validate_loopback_base_url(base_url)
    raw_connect = connect or WebSocketCDPConnection
    transport = ScopedCDPTransport(
        base_url, fetch_json=fetch, connect=lambda ws: ReadOnlyConnection(raw_connect(ws)))
    try:
        bound = transport.bind_page_target(page["id"])
    except TargetBindingError as exc:
        raise InspectorError(str(exc)) from exc
    with bound:
        connection = bound._connection

        def evaluate(expression: str):
            reply = connection.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
            result = reply.get("result", {})
            value = result.get("value") if isinstance(result, dict) else None
            if not isinstance(value, str):
                raise InspectorError("page evaluation returned no text")
            return value

        before = evaluate(LOCATION_EXPRESSION)
        if public_url(before) != page["url"]:
            raise InspectorError("the page changed between listing and inspecting; run again")
        raw = evaluate(INSPECT_EXPRESSION)
        after = evaluate(LOCATION_EXPRESSION)
        if public_url(after) != page["url"]:
            raise InspectorError("the page navigated during inspection; discarding the result")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InspectorError("inspection result was not valid JSON") from exc
    return build_report(
        payload, page_url=before, inspected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def default_output_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "job-agent" / "inspections"


def output_path(directory: Path, report: dict) -> Path:
    host = urlsplit(report["page_url"]).hostname or "page"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{host}{urlsplit(report['page_url']).path}".lower()).strip("-")[:60]
    stamp = report["inspected_at"].replace(":", "").replace("-", "")[:15]
    return directory / f"{stamp}-{slug}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--list", action="store_true", help="list open pages and exit")
    parser.add_argument("--url-contains", help="inspect the one open page whose URL contains this text")
    parser.add_argument("--out-dir", type=Path, default=None, help="where to save the JSON (outside the repo)")
    args = parser.parse_args(argv)
    try:
        pages = list_pages(args.base_url)
        if args.list:
            print(json.dumps(pages, indent=2))
            return 0
        report = inspect_page(args.base_url, choose_page(pages, args.url_contains))
    except InspectorError as exc:
        print(f"inspector: {exc}", file=sys.stderr)
        return 2
    directory = args.out_dir or default_output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = output_path(directory, report)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"saved_to": str(path), "page_url": report["page_url"], **report["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
