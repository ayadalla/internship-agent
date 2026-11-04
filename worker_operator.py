"""Narrow compatibility bridge for the already approved Unix browser worker.

Never starts a worker, opens Chrome/WebSockets, or retries an action.
Preparation mutation requires constructor opt-in; see BROWSER_INTERFACE.md.
See docs/WORKER-OPERATOR.md for client-bound Review, single-use submit and guest evidence.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import socket
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mutable_cdp_page_adapter import WorkerPreparationAdapter


class WorkerClient:
    def __init__(self, path: str):
        self.path = path

    def request(self, payload: dict) -> object:
        encoded = json.dumps(payload).encode("utf-8") + b"\n"
        if len(encoded) >= 65536:
            raise ValueError("worker request exceeds 64-KiB line limit; no request sent")
        metadata = Path(self.path).lstat()
        if (
            not Path(self.path).is_absolute()
            or not stat.S_ISSOCK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("approved worker requires a private same-owner Unix socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(90)
            connection.connect(self.path)
            connection.sendall(encoded)
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline())
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise ValueError("approved worker request failed; do not replay uncertain actions")
        return response.get("data")

    def health(self) -> dict:
        data = self.request({"action": "health"})
        ready = isinstance(data, dict) and data.get("ready") is True
        return {"status": "ready" if ready else "blocked", "transport": "approved_unix_worker"}


class WorkerConnection:
    """Worker attaches per request: only by-value evaluation is session safe."""

    def __init__(self, client: WorkerClient, target: dict, *, official_posting: dict | None = None, observation_only: bool = False):
        self.client = client
        self.target = dict(target)
        self.official_posting = official_posting
        self.observation_only = observation_only

    def call(self, method: str, params: dict) -> dict:
        if (
            method != "Runtime.evaluate"
            or params.get("returnByValue") is not True
            or not isinstance(params.get("expression"), str)
            or set(params) - {"expression", "returnByValue", "awaitPromise"}
        ):
            raise ValueError("worker supports only by-value Runtime.evaluate; session handles are unsafe")
        expression = params["expression"]
        if self.observation_only and expression not in {
            "location.href", "document.title", "document.body ? document.body.innerText : ''",
            "document.documentElement ? document.documentElement.outerHTML : ''",
        }:
            raise ValueError("confirmation transport is observation-only")
        guarded = (
            "(() => { if (location.href !== " + json.dumps(self.target["url"])
            + ") throw new Error('exact worker target URL drift'); return ("
            + expression + "); })()"
        )
        payload = {
            "action": "call", "target": self.target["id"], "prefix": self.target["url"],
            "method": method, "params": {**params, "expression": guarded},
        }
        if len(json.dumps(payload).encode("utf-8")) + 1 >= 65536:
            raise ValueError("approved worker readline limit exceeded; request not sent")
        data = self.client.request(payload)
        binding = data.get("binding") if isinstance(data, dict) else None
        if (
            not isinstance(binding, dict)
            or binding.get("targetId") != self.target["id"]
            or binding.get("url") != self.target["url"]
        ):
            raise ValueError("exact worker response binding drift")
        result = data.get("result")
        if not isinstance(result, dict) or "exceptionDetails" in result or not isinstance(result.get("result"), dict):
            raise ValueError("invalid worker evaluation result; no automatic replay")
        return result

    def close(self) -> None:
        # The worker owns the persistent browser session; never close/detach it.
        pass


class WorkerTransport:
    def __init__(self, client: WorkerClient, *, target: dict, official_posting: dict | None = None, confirmation_context: dict | None = None, allow_mutation: bool = False, preparation_step: str = "application", approved_upload_path: str | None = None):
        if type(allow_mutation) is not bool:
            raise ValueError("preparation mutation opt-in must be an explicit boolean")
        if allow_mutation and confirmation_context is not None:
            raise ValueError("confirmation transport is observation-only")
        self.client = client
        self.allow_mutation = allow_mutation
        self.preparation_step = preparation_step
        self.approved_upload_path = approved_upload_path
        self.target = dict(target)
        self.official_posting = official_posting
        self.observation_only = confirmation_context is not None
        if confirmation_context is not None:
            from submission_authorization import _review_binding, BINDING_KEYS
            from greenhouse_guest_confirmation import is_permitted_confirmation_url
            context = confirmation_context
            binding = _review_binding(context.get("review_evidence", {}), job_id=context.get("job_id"))
            try:
                entries = [json.loads(line) for line in Path(context["journal_path"]).read_text(encoding="utf-8").splitlines() if line.strip()]
                intents = [entry['evidence'] for entry in entries if entry.get('action') == 'submit' and entry.get('evidence',{}).get('status') == 'intent_recorded']
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError("original confirmation journal unavailable") from exc
            transition = context.get("transition", {})
            if not (
                len(intents) == 1 and all(intents[0].get(k) == binding[k] for k in BINDING_KEYS)
                and binding['target_id'] == target['id'] and binding['page_url'] == target['url']
                and transition.get('target_id') == target['id'] and transition.get('from_url') == target['url']
                and transition.get('source') == 'one_shot_same_tab_observation' and transition.get('trusted') is True
                and transition.get('intent_sha256') == hashlib.sha256(json.dumps(intents[0],sort_keys=True,separators=(',', ':')).encode()).hexdigest()
                and is_permitted_confirmation_url(origin_url=target['url'],page_url=transition.get('to_url'),tenant='schonfeld')
            ):
                raise ValueError("exact original journal and same-target transition required")
            self.target = {'id':target['id'], 'url':transition['to_url']}

    def bind_page_target(self, target_id: str):
        from scoped_cdp import BoundPage, TargetBindingError

        targets = self.client.request({"action": "list", "match": [self.target["url"]]})
        matches = [item for item in targets if isinstance(item, dict)
                   and item.get("targetId") == target_id and item.get("type") == "page"
                   and item.get("url") == self.target["url"]]
        if target_id != self.target["id"] or len(matches) != 1:
            raise TargetBindingError("exact worker page target was not found")
        return WorkerPage(target_id=target_id, target_url=self.target["url"], connection=WorkerConnection(
            self.client, self.target, official_posting=self.official_posting, observation_only=self.observation_only))

    def bind_mutable_page_target(self, target_id: str):
        preparation = None
        if self.allow_mutation:
            from mutable_cdp_page_adapter import WorkerPreparationAdapter
            preparation = WorkerPreparationAdapter(
                target_id=self.target["id"], target_url=self.target["url"],
                connection=WorkerConnection(self.client, self.target),
                preparation_step=self.preparation_step, approved_upload_path=self.approved_upload_path)
        bound = self.bind_page_target(target_id)
        bound._preparation = preparation
        return bound


class WorkerPage:
    """Default verification and one-shot submit, with opted-in field preparation.

    Default preparation preserves observed values. The injected preparation
    adapter can replace/select/upload learned fields without native input.
    Its opt-in never grants Submit authority or server Review provenance.
    """

    def __init__(self, *, target_id: str, target_url: str, connection):
        self.target_id = target_id
        self.target_url = target_url
        self._connection = connection
        self._preparation: WorkerPreparationAdapter | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._connection.close()

    def read_only_snapshot(self) -> dict:
        from scoped_cdp import BoundPage
        snapshot = BoundPage(self.target_id, self.target_url, self._connection).read_only_snapshot()
        posting = getattr(self._connection, "official_posting", None)
        if posting is not None:
            snapshot["official_posting"] = posting
        return snapshot

    def inspect_safety_surface(self, selectors: list[str]) -> dict:
        from schonfeld_form import control_expression
        if not selectors:
            raise ValueError("learned canary selectors are required")
        states = [self._evaluate(control_expression(selector)) for selector in selectors]
        if not all(isinstance(state, dict) for state in states):
            raise ValueError("live canary observation unavailable")
        return {
            "observation_source": "live_page_dom",
            "activation_route": "direct_dom",
            "retina_scale": self._evaluate("window.devicePixelRatio"),
            "control_visible": all(state.get("count") == 1 and all(state.get(key) is True
                                     for key in ("visible", "enabled")) for state in states),
            "overlay_present": any(state.get("in_viewport") is not False and state.get("visible") is True
                                   and state.get("unobscured") is not True for state in states),
            # A page-only, read-only worker cannot observe native window state.
            # Direct DOM activation has no native-window interception dependency.
            "native_window_detected": None,
        }

    def observe_bound_form(self) -> dict:
        from schonfeld_form import CONTROLS, REQUIRED, control_expression
        fields = {}
        for field in REQUIRED:
            if field == "resume":
                fields[field] = {"rendered_attachment_present": bool(self.read_uploaded_filename("#resume"))}
                continue
            state = self._evaluate(control_expression(CONTROLS[field][0]))
            fields[field] = {
                key: state.get(key) if isinstance(state, dict) else None
                for key in ("count", "bound", "valid", "visible", "enabled", "unobscured")
            }
        return {"source": "live_bound_form_state", "server_saved": False,
                "review_authoritative": False, "fields": fields}

    def read_greenhouse_server_review(self) -> dict:
        from live_review_reader import LiveReviewReadError
        self.observe_bound_form()
        raise LiveReviewReadError(
            "server_saved_review_unavailable: Schonfeld exposes live_bound_form_state only; "
            "client-bound observations are not server-persisted Review authority"
        )

    def _evaluate(self, expression: str):
        result = self._connection.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            raise ValueError("read-only worker form evaluation failed")
        return result.get("result", {}).get("value")

    def _read(self, selector: str) -> dict:
        from schonfeld_form import control_expression
        state = self._evaluate(control_expression(selector))
        if not isinstance(state, dict) or state.get("count") != 1:
            raise ValueError("exactly one learned control required; parent must repair")
        if not all(state.get(key) is True for key in ("visible", "enabled", "valid", "bound")):
            raise ValueError("visible, enabled, valid React-bound control required; parent must repair")
        return state

    def replace_text(self, selector: str, value: str) -> None:
        if self._preparation is not None:
            return self._preparation.replace_text(selector, value)
        actual = self.read_value(selector)
        if selector == "#phone":
            actual = "".join(c for c in actual if c.isdigit())
            value = "".join(c for c in value if c.isdigit())
        if actual != value:
            raise ValueError("verification-only preparation mismatch; parent must repair")

    def read_value(self, selector: str) -> str:
        if self._preparation is not None:
            return self._preparation.read_value(selector)
        return self._read(selector)["value"]

    def react_select_exact(self, selector: str, search_text: str, exact_option: str) -> None:
        if self._preparation is not None:
            return self._preparation.react_select_exact(selector, search_text, exact_option)
        state = self._read(selector)
        choices = state.get("choice")
        if not isinstance(choices, list) or len(choices) != 1 or choices[0].get("label") != exact_option:
            raise ValueError("exact React-bound option differs; parent must repair")

    def select_option(self, selector: str, value: str) -> None:
        if self._preparation is None:
            raise ValueError("native selection requires explicit preparation mutation opt-in")
        self._preparation.select_option(selector, value)

    def read_selected_option(self, selector: str) -> str:
        if self._preparation is None:
            raise ValueError("native selection requires explicit preparation mutation opt-in")
        return self._preparation.read_selected_option(selector)

    def read_react_selected_option(self, selector: str) -> str:
        if self._preparation is not None:
            return self._preparation.read_react_selected_option(selector)
        return self._read(selector)["value"]

    def read_greenhouse_client_bound_form(self) -> dict:
        from schonfeld_form import client_form_expression, URL
        if self.target_url != URL:
            raise ValueError("client-bound form reader requires exact learned URL")
        raw = self._evaluate(client_form_expression())
        if not isinstance(raw, dict) or raw.get("page_url") != self.target_url:
            raise ValueError("client form observation unavailable or URL drift")
        self._reviewed_client_state = raw
        return {**raw, "target_id": self.target_id}

    def inspect_submit_control(self, selector: str) -> dict:
        from schonfeld_form import submit_expression
        state = self._evaluate(submit_expression(selector))
        if not isinstance(state, dict):
            raise ValueError("submit control observation unavailable")
        return {**state, "selector": selector, "target_id": self.target_id, "url": self.target_url}

    def click_submit_once(self, selector: str) -> None:
        if self._preparation is not None:
            raise ValueError("preparation opt-in never authorizes Submit")
        from schonfeld_form import submit_expression
        from one_shot_submit import SubmitInterrupted
        if getattr(self, "_submit_attempted", False):
            raise ValueError("page submit replay forbidden")
        self._submit_attempted = True
        try:
            self._evaluate(submit_expression(selector, activate=True, expected_state=getattr(self, "_reviewed_client_state", None)))
        except (OSError, ValueError, RuntimeError) as exc:
            raise SubmitInterrupted("worker activation uncertain; inspect without replay") from exc

    def read_after_submit_snapshot(self) -> dict:
        from greenhouse_guest_confirmation import is_permitted_confirmation_url, extract_observed_success
        from scoped_cdp import BoundPage
        import time
        if not getattr(self, "_submit_attempted", False):
            raise ValueError("same-tab observation requires an attempted one-shot submit")
        deadline = time.monotonic() + 20
        while True:
            targets = self._connection.client.request({"action": "list", "match": ["https://job-boards.greenhouse.io/schonfeld/"]})
            matches = [item for item in targets if isinstance(item, dict) and item.get("targetId") == self.target_id
                       and item.get("type") == "page" and is_permitted_confirmation_url(
                           origin_url=self.target_url, page_url=item.get("url"), tenant="schonfeld")]
            if len(matches) != 1:
                raise ValueError("same-target learned confirmation route unavailable")
            bound = {"id": self.target_id, "url": matches[0]["url"]}
            connection = WorkerConnection(self._connection.client, bound, observation_only=True)
            snapshot = BoundPage(self.target_id, bound["url"], connection).read_only_snapshot()
            try:
                extract_observed_success(snapshot)
                return snapshot
            except ValueError:
                if time.monotonic() >= deadline:
                    return snapshot
                time.sleep(0.5)

    def inspect_confirmation(self) -> dict:
        # The separate learned confirmation stage is authoritative, not this click.
        return {"confirmed": False}

    def read_uploaded_filename(self, selector: str) -> str:
        if self._preparation is not None:
            return self._preparation.read_uploaded_filename(selector)
        if selector != "#resume":
            raise ValueError("only the exact resume slot is learned")
        value = self._evaluate("""(() => {
            const groups = [...document.querySelectorAll('[aria-labelledby="upload-label-resume"]')];
            if (groups.length !== 1) return '';
            const group = groups[0], style = getComputedStyle(group);
            if (style.display === 'none' || style.visibility === 'hidden' || !group.getClientRects().length) return '';
            const names = [...group.querySelectorAll('.file-upload__filename p')].filter(e => e.getClientRects().length);
            if (names.length !== 1) return '';
            const filename = names[0].innerText.trim();
            const inputs = [...group.querySelectorAll('input[type="file"]')];
            if (inputs.length > 1) return '';
            if (inputs[0]?.files?.length && (inputs[0].files.length !== 1 || inputs[0].files[0].name !== filename)) return '';
            return filename;
        })()""")
        return value if isinstance(value, str) else ""

    def cdp_upload(self, selector: str, path: str) -> None:
        if self._preparation is not None:
            return self._preparation.cdp_upload(selector, path)
        if self.read_uploaded_filename(selector) != Path(path).name:
            raise ValueError("verification-only bridge: parent must upload through the approved worker; never reuse CDP handles")

    def read_uploaded_sha256(self, selector: str) -> str:
        if self._preparation is None:
            return ""
        return self._preparation.read_uploaded_sha256(selector)

    @property
    def requires_upload_digest(self) -> bool:
        return self._preparation is not None

    def uploaded_file_matches(self, selector: str, path: str) -> bool:
        if self._preparation is None:
            raise ValueError("byte-verified upload requires preparation opt-in")
        return self._preparation.uploaded_file_matches(selector, path)

    def set_checked(self, selector: str, checked: bool) -> None:
        raise ValueError("active consent/checkable controls are prohibited in worker preparation")


def main(argv: list[str] | None = None) -> int:
    """Delegate gated stages unchanged; the HTTP-origin argument is unused by DI."""
    import argparse
    import live_run_manifest
    import production_operator

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--worker-socket", required=True)
    parser.add_argument("--posting-evidence")
    args, operator_args = parser.parse_known_args(argv)
    manifest_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    manifest_parser.add_argument("--manifest", required=True)
    manifest_parser.add_argument("--enable-production-live", action="store_true")
    manifest_args, _ = manifest_parser.parse_known_args(operator_args)
    try:
        if (
            len(operator_args) < 2 or operator_args[0] != "live"
            or operator_args[1] not in {"preflight", "prepare", "review", "authorize", "submit", "confirmation", "status"}
        ):
            raise ValueError("worker wrapper supports gated observation/preparation stages only; no delivery or delivery recovery")
        manifest = live_run_manifest.load_manifest(
            manifest_args.manifest, production_enabled=manifest_args.enable_production_live,
        )
        client = WorkerClient(args.worker_socket)
        posting = json.loads(Path(args.posting_evidence).read_text(encoding="utf-8")) if args.posting_evidence else None
        context = None
        if operator_args[1] == "confirmation":
            from schonfeld_form import URL
            transition_path = Path(manifest["runtime_paths"]["confirmation"] + ".transition.json")
            if manifest["target"]["url"] == URL and transition_path.exists():
                review = json.loads(Path(manifest["runtime_paths"]["review"]).read_text(encoding="utf-8"))
                context = {"job_id":manifest['job_id'], "journal_path":manifest['runtime_paths']['submit_journal'],
                           "review_evidence":review.get('review'), "transition":json.loads(transition_path.read_text(encoding="utf-8"))}
        transport = WorkerTransport(client, target=manifest["target"], official_posting=posting, confirmation_context=context)
        return production_operator.main(
            operator_args,
            live_transport_factory=lambda _origin: transport,
            live_readonly_transport_factory=lambda _origin: transport,
            live_health_probe=lambda _origin: client.health(),
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc), "submission_enabled": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
