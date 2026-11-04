"""Offline tests only: disposable Unix socket peers, never the browser worker."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from contextlib import contextmanager

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"),
    reason="worker bridge uses Unix domain sockets; no Windows transport yet",
)

URL = "https://job-boards.greenhouse.io/schonfeld/jobs/8171772"


def module():
    assert importlib.util.find_spec("worker_operator"), "worker_operator bridge is missing"
    import worker_operator
    return worker_operator


@contextmanager
def local_worker(replies):
    """Protocol fixture, not live evidence; no Chrome or TCP connection."""
    requests = []
    errors = []
    with tempfile.TemporaryDirectory(prefix="wo-", dir="/tmp") as directory:
        path = str(Path(directory) / "worker.sock")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen()
            server.settimeout(3)

            def serve():
                try:
                    for reply in replies:
                        connection, _ = server.accept()
                        with connection, connection.makefile("rb") as stream:
                            requests.append(json.loads(stream.readline()))
                            connection.sendall(json.dumps(reply).encode() + b"\n")
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=serve)
            thread.start()
            try:
                yield path, requests
            finally:
                thread.join(timeout=4)
                assert not thread.is_alive()
                assert not errors, errors


def test_worker_health_uses_existing_unix_protocol_not_http():
    bridge = module()
    with local_worker([{"ok": True, "data": {"ready": True, "browser": "offline fixture"}}]) as (path, requests):
        health = bridge.WorkerClient(path).health()
    assert requests == [{"action": "health"}]
    assert health == {"status": "ready", "transport": "approved_unix_worker"}


def test_worker_refuses_non_private_socket_before_connection(tmp_path):
    bridge = module()
    path = tmp_path / "not-a-socket"
    path.write_text("not a worker", encoding="utf-8")
    with pytest.raises(ValueError, match="private same-owner Unix socket"):
        bridge.WorkerClient(str(path)).health()


def test_exact_worker_binding_reads_page_without_cdp_object_handles():
    bridge = module()
    assert hasattr(bridge, "WorkerTransport"), "exact worker transport is missing"
    binding = {"targetId": "target-1", "url": URL, "windowId": 4}
    replies = [{"ok": True, "data": [{"targetId": "target-1", "type": "page", "url": URL}]}]
    replies += [{"ok": True, "data": {"binding": binding, "result": {"result": {"value": value}}}} for value in [URL, "Schonfeld", "Fixture body", "<html>fixture</html>"]]
    with local_worker(replies) as (path, requests):
        transport = bridge.WorkerTransport(bridge.WorkerClient(path), target={"id": "target-1", "url": URL})
        with transport.bind_page_target("target-1") as page:
            snapshot = page.read_only_snapshot()
    assert snapshot["url"] == URL
    assert snapshot["html"] == "<html>fixture</html>"
    assert snapshot["read_only"] is True
    assert requests[0] == {"action": "list", "match": [URL]}
    for request in requests[1:]:
        assert request["action"] == "call"
        assert request["method"] == "Runtime.evaluate"
        assert request["target"] == "target-1"
        assert request["prefix"] == URL
        assert request["params"]["returnByValue"] is True
        assert "location.href !== " + json.dumps(URL) in request["params"]["expression"]


@pytest.mark.parametrize("binding", [None, {"targetId": "other", "url": URL}, {"targetId": "target-1", "url": URL + "0"}])
def test_worker_rejects_response_binding_drift(binding):
    bridge = module()
    with local_worker([{"ok": True, "data": {"binding": binding, "result": {"result": {"value": "wrong page"}}}}]) as (path, requests):
        connection = bridge.WorkerConnection(bridge.WorkerClient(path), {"id": "target-1", "url": URL})
        with pytest.raises(ValueError, match="worker response binding"):
            connection.call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
    assert len(requests) == 1


@pytest.mark.parametrize("method,params", [
    ("DOM.requestNode", {"objectId": "stale"}),
    ("DOM.setFileInputFiles", {"nodeId": 1, "files": ["not-a-real-resume"]}),
    ("Runtime.evaluate", {"expression": "document.body", "returnByValue": False}),
])
def test_worker_refuses_session_scoped_handles_without_request(method, params):
    bridge = module()

    class NoRequests:
        def request(self, payload):
            pytest.fail("unsafe worker request")

    with pytest.raises(ValueError, match="only by-value Runtime.evaluate"):
        bridge.WorkerConnection(NoRequests(), {"id": "target-1", "url": URL}).call(method, params)


def test_wrapper_runs_existing_preflight_using_injected_worker_only(tmp_path, capsys):
    bridge = module()
    assert hasattr(bridge, "main"), "production main wrapper is missing"
    from test_production_operator_live import _write_live_inputs
    manifest_path, _, manifest = _write_live_inputs(tmp_path)
    manifest["target"]["url"] = URL
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    html = (ROOT / "fixtures" / "local_operator_e2e.html").read_text(encoding="utf-8")
    replies = [{"ok": True, "data": {"ready": True}}]
    replies += [{"ok": True, "data": [{"targetId": "target-abc", "url": URL, "type": "page"}]}]
    replies += [{"ok": True, "data": {"binding": {"targetId": "target-abc", "url": URL}, "result": {"result": {"value": value}}}} for value in [URL, "Fixture", "Fixture body", html] * 2]
    with local_worker(replies) as (path, requests):
        code = bridge.main(["--worker-socket", path, "live", "preflight", "--manifest", str(manifest_path)])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["identity_verified"] is True
    assert result["submission_enabled"] is False
    assert len(requests) == 10


def test_worker_prepare_verifies_existing_text_without_mutating_it():
    bridge = module()
    assert hasattr(bridge, "WorkerPage"), "verification-only page adapter is missing"
    calls = []

    class ObservedText:
        def call(self, method, params):
            calls.append((method, params))
            return {"result": {"value": {
                "count": 1, "value": "Fixture", "bound": True, "valid": True,
                "visible": True, "enabled": True, "unobscured": True,
            }}}

    page = bridge.WorkerPage(target_id="target-1", target_url=URL, connection=ObservedText())
    page.replace_text("#first_name", "Fixture")
    assert page.read_value("#first_name") == "Fixture"
    with pytest.raises(ValueError, match="parent must repair"):
        page.replace_text("#first_name", "Different")
    for method, params in calls:
        assert method == "Runtime.evaluate"
        for mutation in ["dispatchEvent", ".click(", ".focus(", "scrollIntoView", "Object.defineProperty"]:
            assert mutation not in params["expression"]


def test_worker_page_preserves_uploaded_resume_without_a_file_input():
    bridge = module()
    import browser_actions
    assert hasattr(bridge.WorkerPage, "read_uploaded_filename"), "attachment verification is missing"
    expressions = []

    class Attachment:
        def call(self, method, params):
            expressions.append(params["expression"])
            return {"result": {"value": "Resume.pdf"}}

    page = bridge.WorkerPage(target_id="target-1", target_url=URL, connection=Attachment())
    assert browser_actions.cdp_upload(page, "#resume", "/offline-fixture/Resume.pdf")["verified"] is True
    with pytest.raises(ValueError, match="parent must upload"):
        page.cdp_upload("#resume", "/offline-fixture/Different.pdf")
    assert all('upload-label-resume' in expression for expression in expressions)
    assert all('file-upload__filename p' in expression for expression in expressions)
    assert all('setFileInputFiles' not in expression for expression in expressions)


def test_worker_mutable_seam_retains_server_review_blocker_after_form_observation():
    bridge = module()
    import live_review_reader
    assert hasattr(bridge.WorkerTransport, "bind_mutable_page_target"), "operator page seam is missing"
    requests = []

    class ReadOnlyFixture:
        def request(self, payload):
            requests.append(payload)
            if payload["action"] == "list":
                return [{"targetId": "target-1", "url": URL, "type": "page"}]
            return {"binding": {"targetId": "target-1", "url": URL}, "result": {"result": {"value": {
                "count": 1, "value": "Fixture", "bound": True, "valid": True,
                "visible": True, "enabled": True, "unobscured": True,
            }}}}

    transport = bridge.WorkerTransport(ReadOnlyFixture(), target={"id": "target-1", "url": URL})
    with transport.bind_mutable_page_target("target-1") as page:
        observation = page.observe_bound_form()
        assert observation["source"] == "live_bound_form_state"
        assert observation["server_saved"] is False
        assert observation["review_authoritative"] is False
        import schonfeld_form
        assert set(observation["fields"]) == set(schonfeld_form.REQUIRED)
        with pytest.raises(live_review_reader.LiveReviewReadError, match="server_saved_review_unavailable"):
            page.read_greenhouse_server_review()
    for request in requests[1:]:
        expression = request["params"]["expression"]
        assert "dispatchEvent" not in expression
        assert ".click(" not in expression


def test_worker_page_snapshot_and_canary_are_observation_only():
    bridge = module()
    assert hasattr(bridge.WorkerPage, "read_only_snapshot"), "snapshot compatibility is missing"
    assert hasattr(bridge.WorkerPage, "inspect_safety_surface"), "canary compatibility is missing"
    from scoped_cdp import BoundPage

    class Observations:
        def call(self, method, params):
            expression = params["expression"]
            assert "scrollIntoView" not in expression
            assert "dispatchEvent" not in expression
            values = {"location.href": URL, "document.title": "Fixture", "document.body ? document.body.innerText : ''": "Body", "document.documentElement ? document.documentElement.outerHTML : ''": "<html>offline</html>", "window.devicePixelRatio": 2}
            return {"result": {"value": values.get(expression, {"count": 1, "visible": False, "enabled": True, "unobscured": False})}}

    page = bridge.WorkerPage(target_id="target-1", target_url=URL, connection=Observations())
    assert page.read_only_snapshot() == BoundPage("target-1", URL, Observations()).read_only_snapshot()
    surface = page.inspect_safety_surface(["#first_name"])
    assert surface["control_visible"] is False
    assert surface["native_window_detected"] is None, "page-only observation cannot certify native windows absent"


def test_wrapper_disallows_external_delivery_before_any_manifest_or_worker_access(capsys):
    bridge = module()
    code = bridge.main(["--worker-socket", "/not-used", "live", "deliver", "--manifest", "/not-used"])
    assert code == 2
    assert "worker wrapper supports gated observation/preparation stages only" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.parametrize("result", [None, [], {"exceptionDetails": {"text": "fixture error"}}])
def test_worker_rejects_invalid_evaluation_results_without_replay(result):
    bridge = module()
    with local_worker([{"ok": True, "data": {"binding": {"targetId": "target-1", "url": URL}, "result": result}}]) as (path, requests):
        with pytest.raises(ValueError, match="worker evaluation result"):
            bridge.WorkerConnection(bridge.WorkerClient(path), {"id": "target-1", "url": URL}).call("Runtime.evaluate", {"expression": "document.title", "returnByValue": True})
    assert len(requests) == 1
