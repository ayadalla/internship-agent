"""Process-scoped real Chrome/CDP harness for sanitized static integration tests."""
from __future__ import annotations

import base64
import json
import os
import select
import signal
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no fcntl; the pipe-based launcher below is POSIX-only.
    fcntl = None

from mutable_cdp_page_adapter import MutableCDPPageAdapter, PageControlError
from scoped_cdp import BoundPage


class LocalCDPIntegrationError(RuntimeError):
    """The sanitized local Chrome integration target could not be established."""


class _PipeCDPChannel:
    """Synchronous null-delimited CDP channel over Chrome's dedicated pipe FDs."""

    def __init__(self, *, write_fd: int, read_fd: int) -> None:
        self._write_fd = write_fd
        self._read_fd = read_fd
        self._sequence = 0
        self._buffer = b""

    def _read_message(self, timeout_seconds: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout_seconds
        while b"\0" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LocalCDPIntegrationError("Chrome CDP pipe response timed out")
            ready, _, _ = select.select([self._read_fd], [], [], remaining)
            if not ready:
                raise LocalCDPIntegrationError("Chrome CDP pipe response timed out")
            chunk = os.read(self._read_fd, 65536)
            if not chunk:
                raise LocalCDPIntegrationError("Chrome closed the CDP pipe")
            self._buffer += chunk
        encoded, self._buffer = self._buffer.split(b"\0", 1)
        try:
            message = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LocalCDPIntegrationError("Chrome returned malformed CDP pipe data") from exc
        if not isinstance(message, dict):
            raise LocalCDPIntegrationError("Chrome returned a non-object CDP message")
        return message

    def call(self, method: str, params: dict, *, session_id: str | None = None) -> dict:
        self._sequence += 1
        request_id = self._sequence
        request: dict[str, object] = {
            "id": request_id,
            "method": method,
            "params": params,
        }
        if session_id is not None:
            request["sessionId"] = session_id
        os.write(
            self._write_fd,
            json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\0",
        )
        while True:
            reply = self._read_message()
            if reply.get("id") != request_id:
                continue
            if "error" in reply:
                raise RuntimeError(reply["error"])
            result = reply.get("result", {})
            if not isinstance(result, dict):
                raise RuntimeError("CDP returned a non-object result")
            return result

    def close(self) -> None:
        for descriptor in (self._write_fd, self._read_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


class _PipeTargetConnection:
    """One flattened exact-target session on a shared process-scoped CDP pipe."""

    def __init__(self, channel: _PipeCDPChannel, session_id: str) -> None:
        self._channel = channel
        self._session_id = session_id
        self._closed = False

    def call(self, method: str, params: dict) -> dict:
        if self._closed:
            raise LocalCDPIntegrationError("exact CDP target session is closed")
        return self._channel.call(method, params, session_id=self._session_id)

    def close(self) -> None:
        if not self._closed:
            self._channel.call(
                "Target.detachFromTarget", {"sessionId": self._session_id}
            )
            self._closed = True


def find_local_chrome_for_testing() -> Path:
    """Find an installed local Chrome binary, preferring hermetic Chrome-for-Testing."""
    playwright_cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    cached = sorted(
        playwright_cache.glob(
            "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell"
        ),
        reverse=True,
    )
    for candidate in cached:
        if candidate.is_file():
            return candidate
    system_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if system_chrome.is_file():
        return system_chrome
    raise LocalCDPIntegrationError("no local Chrome executable is available")


class LocalExactCDPPage(MutableCDPPageAdapter):
    """Exact loopback fixture page with bounded test-only Review/submit methods."""

    def __init__(self, bound: BoundPage) -> None:
        super().__init__(
            target_id=bound.target_id,
            target_url=bound.target_url,
            connection=bound._connection,
        )
        self._bound = bound

    def __enter__(self) -> "LocalExactCDPPage":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self._bound.__exit__(exc_type, exc_value, traceback)

    def _fixture_state(self) -> dict:
        value = self._evaluate(
            "(() => { const body = document.body; const identity = document.querySelector('#identity'); "
            "return { fixture: body && body.dataset.fixture, "
            "identity: identity ? { company: identity.dataset.company, role: identity.dataset.role, requisition: identity.dataset.requisition } : {}, "
            "gates: [...document.querySelectorAll('[data-gate]')].map(item => item.dataset.gate), "
            "maango: body && body.dataset.maango === 'true', "
            "overlay_present: getComputedStyle(document.querySelector('#fixture-overlay')).display !== 'none', "
            "device_scale_factor: window.devicePixelRatio }; })()"
        )
        if not isinstance(value, dict) or value.get("fixture") != "local-operator":
            raise LocalCDPIntegrationError("exact page is not the sanitized local operator fixture")
        return value

    def read_only_snapshot(self) -> dict[str, object]:
        self._fresh_target()
        state = self._fixture_state()
        html = self._evaluate(
            "document.documentElement ? document.documentElement.outerHTML : ''"
        )
        if not isinstance(html, str):
            raise LocalCDPIntegrationError("sanitized fixture HTML was unavailable")
        return {
            "target_id": self.target_id,
            "url": self.target_url,
            "html": html,
            "read_only": True,
            "identity": state["identity"],
            "gates": state["gates"],
            "maango": state["maango"],
            "overlay_present": state["overlay_present"],
            "device_scale_factor": state["device_scale_factor"],
        }

    def set_retina_scale_for_test(self, scale: float) -> None:
        if scale <= 0:
            raise ValueError("positive test device scale is required")
        self._fresh_target()
        self._connection.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": 1280,
                "height": 800,
                "deviceScaleFactor": scale,
                "mobile": False,
            },
        )

    def toggle_overlay_for_test(self, visible: bool) -> None:
        self._fresh_target()
        self._evaluate(f"window.fixtureToggleOverlay({str(bool(visible)).lower()})")

    def capture_scoped_screenshot(self) -> bytes:
        self._fresh_target()
        response = self._connection.call(
            "Page.captureScreenshot", {"format": "png", "fromSurface": True}
        )
        encoded = response.get("data")
        if not isinstance(encoded, str):
            raise LocalCDPIntegrationError("exact-page screenshot was unavailable")
        return base64.b64decode(encoded)

    def _control_state(self, selector: str) -> dict:
        value = self._evaluate(
            "(() => { const selector = " + json.dumps(selector) + "; "
            "const matches = [...document.querySelectorAll(selector)]; const element = matches[0]; "
            "if (!element) return {selector, count: 0}; const style = getComputedStyle(element); "
            "const rect = element.getBoundingClientRect(); "
            "const visible = style.display !== 'none' && style.visibility !== 'hidden' && element.getClientRects().length > 0; "
            "const topElement = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2); "
            "const unobscured = topElement === element || element.contains(topElement); "
            "return { selector, count: matches.length, visible, enabled: !element.disabled, unobscured, "
            "role: element.getAttribute('role') || (element.tagName === 'BUTTON' ? 'button' : '') }; })()"
        )
        return value if isinstance(value, dict) else {}

    def inspect_submit_control(self, selector: str) -> dict:
        self._fresh_target()
        state = self._control_state(selector)
        return {
            "selector": selector,
            "target_id": self.target_id,
            "url": self.target_url,
            "visible": state.get("visible") is True and state.get("unobscured") is True,
            "enabled": state.get("enabled") is True,
            "unique": state.get("count") == 1,
            "role": state.get("role"),
        }

    def _activate_fixture_button(self, selector: str, *, allowed_selector: str) -> None:
        if selector != allowed_selector:
            raise PageControlError("unexpected sanitized fixture control")
        self._fresh_target()
        state = self._control_state(selector)
        if not (
            state.get("count") == 1
            and state.get("visible") is True
            and state.get("enabled") is True
            and state.get("unobscured") is True
            and state.get("role") == "button"
        ):
            raise PageControlError("fixture control must be one visible, enabled, unobscured button")
        self._evaluate(
            "document.querySelector(" + json.dumps(selector) + ").click()"
        )

    def activate_review(self, selector: str) -> None:
        self._activate_fixture_button(selector, allowed_selector="#review")

    def click_submit_once(self, selector: str) -> None:
        self._activate_fixture_button(selector, allowed_selector="#submit")

    def inspect_confirmation(self) -> dict:
        self._fresh_target()
        value = self._evaluate(
            "(() => { const confirmation = document.querySelector('#confirmation'); "
            "const confirmed = document.body.dataset.submitted === 'true' && confirmation && !confirmation.hidden; "
            "return {confirmed: Boolean(confirmed), state: confirmed ? 'submitted' : 'unknown'}; })()"
        )
        return value if isinstance(value, dict) else {"confirmed": False, "state": "unknown"}

    def read_submit_count(self) -> int:
        value = self._evaluate("Number(document.body.dataset.submitCount || '0')")
        return value if isinstance(value, int) else 0

    def wait_for_resume_sha256(self, expected_sha256: str, timeout_seconds: float = 5.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            value = self._evaluate("document.body.dataset.resumeSha256 || ''")
            if value == expected_sha256:
                return
            time.sleep(0.02)
        raise LocalCDPIntegrationError("sanitized resume hash did not render before timeout")

    def read_server_review(self) -> dict:
        self._fresh_target()
        resume = self._evaluate(
            "({basename: document.body.dataset.resumeName || '', sha256: document.body.dataset.resumeSha256 || ''})"
        )
        identity = self._fixture_state()["identity"]
        authorization = self.read_value("#authorization")
        return {
            "target_id": self.target_id,
            "page_url": self.target_url,
            "identity": identity,
            "fields": {
                "#first_name": self.read_value("#first_name"),
                "#last_name": self.read_value("#last_name"),
                "#email": self.read_value("#email"),
                "#phone": self.read_value("#phone"),
                "#authorization": authorization,
                "#sponsorship": self.read_value("#sponsorship"),
            },
            "resume": resume,
            "parser_repairs": [],
            "questions": [{
                "id": "work_authorization",
                "required": True,
                "answered": bool(authorization),
                "verified": authorization in {"Yes", "No"},
            }],
        }

    def read_candidate_applications(self) -> list[dict]:
        self._fresh_target()
        value = self._evaluate(
            "[...document.querySelectorAll('#candidate-applications [data-requisition]')].map(item => ({"
            "platform: item.dataset.platform, company: item.dataset.company, role: item.dataset.role, "
            "requisition: item.dataset.requisition, state: item.dataset.state, submitted: item.dataset.submitted === 'true'}))"
        )
        return value if isinstance(value, list) else []

    def drift_url_for_test(self) -> None:
        self._fresh_target()
        self._evaluate(
            "history.replaceState({}, '', location.pathname + '?requisition=OTHER')"
        )


class LocalChromeFixtureSession:
    """Launch one headless Chrome target with process-scoped pipe CDP."""

    def __init__(self, *, fixture_path: Path, chrome_path: Path, runtime_dir: Path) -> None:
        self.fixture_path = Path(fixture_path).resolve()
        self.chrome_path = Path(chrome_path)
        self.runtime_dir = Path(runtime_dir)
        self._pid: int | None = None
        self._wait_status: int | None = None
        self._channel: _PipeCDPChannel | None = None
        self.browser_product = ""
        self.page_url = ""
        self.target_id = ""

    @staticmethod
    def _high_fd(descriptor: int) -> int:
        high_descriptor = fcntl.fcntl(descriptor, fcntl.F_DUPFD, 10)
        os.close(descriptor)
        return high_descriptor

    def _poll(self) -> bool:
        if self._pid is None or self._wait_status is not None:
            return self._wait_status is not None
        waited_pid, status = os.waitpid(self._pid, os.WNOHANG)
        if waited_pid:
            self._wait_status = status
            return True
        return False

    def __enter__(self) -> "LocalChromeFixtureSession":
        if fcntl is None:
            raise LocalCDPIntegrationError(
                "the pipe-based local Chrome fixture session is not supported on Windows yet"
            )
        if not self.chrome_path.is_file():
            raise LocalCDPIntegrationError("Google Chrome executable is unavailable")
        fixture_text = self.fixture_path.read_text()
        if 'data-fixture="local-operator"' not in fixture_text:
            raise LocalCDPIntegrationError("only the sanitized local operator fixture is allowed")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.page_url = self.fixture_path.as_uri() + "?requisition=REQ-123"
        profile_dir = self.runtime_dir / "profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.chrome_path),
            "--headless",
            "--single-process",
            "--no-zygote",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--disable-dev-shm-usage",
            "--disable-features=NetworkService,NetworkServiceInProcess",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-proxy-server",
            self.page_url,
        ]
        input_read, input_write = (
            self._high_fd(descriptor) for descriptor in os.pipe()
        )
        output_read, output_write = (
            self._high_fd(descriptor) for descriptor in os.pipe()
        )
        devnull = os.open(os.devnull, os.O_RDWR)
        file_actions = [
            (os.POSIX_SPAWN_DUP2, input_read, 3),
            (os.POSIX_SPAWN_DUP2, output_write, 4),
            (os.POSIX_SPAWN_DUP2, devnull, 0),
            (os.POSIX_SPAWN_DUP2, devnull, 1),
            (os.POSIX_SPAWN_DUP2, devnull, 2),
        ]
        try:
            self._pid = os.posix_spawn(
                str(self.chrome_path), command, os.environ.copy(), file_actions=file_actions
            )
        finally:
            os.close(input_read)
            os.close(output_write)
            os.close(devnull)
        self._channel = _PipeCDPChannel(write_fd=input_write, read_fd=output_read)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._poll():
                raise LocalCDPIntegrationError("headless Chrome exited before CDP startup")
            try:
                version = self._channel.call("Browser.getVersion", {})
                response = self._channel.call("Target.getTargets", {})
            except LocalCDPIntegrationError:
                time.sleep(0.02)
                continue
            product = version.get("product")
            if not isinstance(product, str) or "Chrome" not in product:
                raise LocalCDPIntegrationError("local browser did not identify as Chrome")
            self.browser_product = product
            targets = response.get("targetInfos", [])
            match = next(
                (
                    item
                    for item in targets
                    if isinstance(item, dict)
                    and item.get("type") == "page"
                    and item.get("url") == self.page_url
                ),
                None,
            ) if isinstance(targets, list) else None
            if isinstance(match, dict) and isinstance(match.get("targetId"), str):
                self.target_id = match["targetId"]
                return self
            time.sleep(0.02)
        raise LocalCDPIntegrationError("exact sanitized fixture target was not found")

    def bind_exact_page(self, target_id: str) -> LocalExactCDPPage:
        if target_id != self.target_id:
            raise LocalCDPIntegrationError("requested target ID is not the exact fixture target")
        if self._channel is None:
            raise LocalCDPIntegrationError("Chrome CDP pipe is not available")
        targets = self._channel.call("Target.getTargets", {}).get("targetInfos", [])
        target = next(
            (
                item
                for item in targets
                if isinstance(item, dict)
                and item.get("targetId") == target_id
                and item.get("type") == "page"
            ),
            None,
        ) if isinstance(targets, list) else None
        if not isinstance(target, dict) or target.get("url") != self.page_url:
            raise LocalCDPIntegrationError("fixture target URL changed before binding")
        attached = self._channel.call(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        session_id = attached.get("sessionId")
        if not isinstance(session_id, str):
            raise LocalCDPIntegrationError("exact fixture target attachment failed")
        bound = BoundPage(
            target_id,
            self.page_url,
            _PipeTargetConnection(self._channel, session_id),
        )
        return LocalExactCDPPage(bound)

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
        if self._pid is None or self._poll():
            return
        os.kill(self._pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self._poll():
            time.sleep(0.02)
        if not self._poll():
            os.kill(self._pid, signal.SIGKILL)
            _, self._wait_status = os.waitpid(self._pid, 0)
