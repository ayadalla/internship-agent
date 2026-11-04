"""Offline tests for the Windows Chrome launcher's pure decisions (no browser is started)."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import windows_chrome as wc

ENV = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
       "LOCALAPPDATA": "/users/me/AppData/Local"}


def test_chrome_candidates_cover_the_usual_install_locations_in_order():
    found = [str(p) for p in wc.chrome_candidates(ENV)]
    assert len(found) == 3 and found[0].startswith("C:") and "Program Files" in found[0]
    assert "(x86)" in found[1] and found[2].endswith("chrome.exe")


def test_chrome_candidates_skip_unset_variables():
    assert wc.chrome_candidates({}) == []


def test_default_profile_lives_under_local_app_data_not_the_repo():
    profile = wc.default_profile_dir(ENV)
    assert profile.parts[-2:] == ("job-agent", "chrome-profile")
    assert wc.profile_problem(profile, ENV, repo_root=Path("/dev/internship-agent")) is None


def test_profile_inside_onedrive_is_refused():
    bad = Path("/users/me/OneDrive/Desktop/chrome-profile")
    assert "OneDrive" in wc.profile_problem(bad, ENV, repo_root=Path("/dev/internship-agent"))


def test_profile_inside_the_repo_is_refused():
    repo = Path("/dev/internship-agent")
    assert "repository" in wc.profile_problem(repo / "runtime" / "chrome", ENV, repo_root=repo)


def test_everyday_chrome_profile_is_refused():
    everyday = Path(ENV["LOCALAPPDATA"]) / "Google" / "Chrome" / "User Data"
    assert "everyday" in wc.profile_problem(everyday, ENV, repo_root=Path("/dev/internship-agent"))


def test_command_is_loopback_only_with_its_own_profile():
    command = wc.build_command(Path("chrome.exe"), 9222, Path("/p/profile"))
    assert command[0] == "chrome.exe"
    assert "--remote-debugging-port=9222" in command
    assert "--remote-debugging-address=127.0.0.1" in command
    assert any(arg.startswith("--user-data-dir=") and arg.endswith("profile") for arg in command)
    assert not any("headless" in arg for arg in command)


class FakeSocket:
    """Stands in for a TCP socket: the offline guard forbids real connections."""
    result = 0
    seen = []

    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def settimeout(self, seconds): pass

    def connect_ex(self, address):
        FakeSocket.seen.append(address)
        return FakeSocket.result


def test_port_in_use_reports_a_listener_and_a_free_port(monkeypatch):
    monkeypatch.setattr(wc.socket, "socket", FakeSocket)
    FakeSocket.seen = []
    FakeSocket.result = 0          # connect succeeded: something is listening
    assert wc.port_in_use(9222) is True
    FakeSocket.result = 10061      # connection refused: free
    assert wc.port_in_use(9222) is False
    assert FakeSocket.seen == [("127.0.0.1", 9222)] * 2   # loopback only, never another host
