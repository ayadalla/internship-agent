"""Start a dedicated Chrome for the agent on Windows, reachable only from this PC.

    .\\.venv\\Scripts\\python.exe tools\\windows_chrome.py            # start and verify
    .\\.venv\\Scripts\\python.exe tools\\windows_chrome.py --check    # only verify what is running

What it does: opens a normal, visible Chrome window using its OWN profile folder
and turns on Chrome's remote-debugging port on 127.0.0.1 (loopback only), which
is how the agent's "live" stages attach. It never touches your everyday Chrome
profile, never closes anything, and starts no agent stage.

Why a separate profile: Chrome 136+ ignores the debugging port when it runs on
the default profile, and anything that can reach that port can drive the browser
and read every site you are signed in to. So this profile is for the agent only:
do not sign in to your personal accounts in it.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_PORT = 9222  # the default of every live stage (--cdp-base-url)


def chrome_candidates(env) -> list[Path]:
    """Usual install locations of chrome.exe, most common first."""
    found = []
    for variable, suffix in (("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
                             ("ProgramFiles(x86)", r"Google\Chrome\Application\chrome.exe"),
                             ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe")):
        base = env.get(variable)
        if base:
            found.append(Path(base) / suffix)
    return found


def default_profile_dir(env) -> Path:
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "job-agent" / "chrome-profile"


def profile_problem(profile: Path, env, repo_root: Path = ROOT) -> str | None:
    """Return why a profile folder is a bad idea, or None if it is fine."""
    resolved = Path(os.path.abspath(profile))
    lowered = [part.casefold() for part in resolved.parts]
    if any("onedrive" in part for part in lowered):
        return "it is inside OneDrive, which would upload the browser's cookies and logins"
    try:
        resolved.relative_to(Path(os.path.abspath(repo_root)))
        return "it is inside the repository folder, where browser data could be committed"
    except ValueError:
        pass
    local = env.get("LOCALAPPDATA")
    if local and resolved == Path(os.path.abspath(Path(local) / "Google" / "Chrome" / "User Data")):
        return "it is your everyday Chrome profile; Chrome ignores the debugging port there"
    return None


def build_command(chrome: Path, port: int, profile: Path) -> list[str]:
    return [
        str(chrome),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_until_ready(base_url: str, seconds: float = 20.0) -> dict:
    import browser_health
    deadline = time.monotonic() + seconds
    payload: dict = {"status": "unreachable"}
    while time.monotonic() < deadline:
        payload = browser_health.probe_cdp_health(base_url)
        if payload.get("status") == "ready":
            return payload
        time.sleep(0.5)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--profile-dir", type=Path, default=None)
    parser.add_argument("--chrome", type=Path, default=None, help="path to chrome.exe if not in a usual place")
    parser.add_argument("--check", action="store_true", help="only verify the debugging port; start nothing")
    args = parser.parse_args(argv)
    base_url = f"http://127.0.0.1:{args.port}"

    if args.check:
        payload = wait_until_ready(base_url, seconds=3)
        print(json.dumps(payload, indent=2))
        return 0 if payload.get("status") == "ready" else 1

    if os.name != "nt":
        print("This launcher is for Windows.", file=sys.stderr)
        return 2
    chrome = args.chrome or next((c for c in chrome_candidates(os.environ) if c.is_file()), None)
    if chrome is None or not Path(chrome).is_file():
        print("Chrome was not found. Install Chrome or pass --chrome <path to chrome.exe>.", file=sys.stderr)
        return 2
    profile = args.profile_dir or default_profile_dir(os.environ)
    problem = profile_problem(profile, os.environ)
    if problem:
        print(f"Refusing profile folder {profile}: {problem}.", file=sys.stderr)
        return 2
    if port_in_use(args.port):
        print(f"Port {args.port} is already in use. If that is the agent's Chrome, run with --check. "
              "Otherwise close whatever uses it; this tool never closes anything for you.", file=sys.stderr)
        return 2

    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(build_command(Path(chrome), args.port, profile), close_fds=True)
    payload = wait_until_ready(base_url)
    print(json.dumps(payload, indent=2))
    if payload.get("status") != "ready":
        print("Chrome started but the debugging port did not answer. Leave the window open and paste this output.",
              file=sys.stderr)
        return 1
    print(f"\nReady. Profile folder: {profile}\n"
          "Do not sign in to personal accounts in this Chrome window. Close the window to stop it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
