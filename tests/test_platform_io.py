"""Offline tests for the OS-difference helpers (Windows branches simulated on any OS)."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import platform_io
from symlink_support import symlink_or_skip


def test_fsync_directory_skips_on_windows_without_touching_the_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_io, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_io.os, "open", lambda *a, **k: pytest.fail("must not open a directory on Windows"))
    platform_io.fsync_directory(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
def test_fsync_directory_flushes_and_closes_on_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_io, "IS_WINDOWS", False)
    synced, closed = [], []
    real_fsync, real_close = os.fsync, os.close
    monkeypatch.setattr(platform_io.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    monkeypatch.setattr(platform_io.os, "close", lambda fd: closed.append(fd) or real_close(fd))
    platform_io.fsync_directory(tmp_path)
    assert len(synced) == 1 and closed == synced


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync")
def test_fsync_directory_still_raises_real_errors_on_posix(tmp_path):
    with pytest.raises(FileNotFoundError):
        platform_io.fsync_directory(tmp_path / "missing")


def test_refuse_symlink_allows_regular_and_absent_paths(tmp_path):
    regular = tmp_path / "file.lock"
    regular.write_text("", encoding="utf-8")
    platform_io.refuse_symlink(regular)
    platform_io.refuse_symlink(tmp_path / "not-created-yet")


def test_refuse_symlink_rejects_a_symlink(tmp_path):
    target = tmp_path / "target"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link"
    symlink_or_skip(link, target)
    with pytest.raises(OSError, match="refusing to follow link"):
        platform_io.refuse_symlink(link)


def test_refuse_symlink_rejects_a_junction(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_io.os.path, "isjunction", lambda p: True, raising=False)
    with pytest.raises(OSError, match="refusing to follow link"):
        platform_io.refuse_symlink(tmp_path)


# ---- owner-only policy -----------------------------------------------------

USER = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1002"


def sddl(owner=USER, dacl="D:PAI(A;;FA;;;%s)" % USER):
    return f"O:{owner}{dacl}"


@pytest.mark.parametrize("text", [
    sddl(),                                                        # only the user
    sddl(dacl=f"D:P(A;;FA;;;{USER})(A;;FA;;;SY)(A;;FA;;;BA)"),     # + SYSTEM, Administrators (aliases)
    sddl(dacl=f"D:P(A;;FA;;;{USER})(A;;FA;;;S-1-5-18)"),           # SYSTEM written as a SID
    sddl(owner="BA", dacl=f"D:P(A;;FA;;;{USER})"),                 # elevated process: Administrators own it
    sddl(dacl=f"D:P(A;;FA;;;{USER})(D;;FA;;;WD)"),                 # deny entries only remove access
    sddl(dacl=f"D:P(A;;FA;;;{USER})(A;OICIIO;FA;;;WD)"),           # inherit-only: applies to children, not the file
])
def test_owner_only_descriptors_are_private(text):
    assert platform_io.sddl_is_private(text, USER) is True


@pytest.mark.parametrize("text", [
    sddl(dacl=f"D:PAI(A;;FA;;;{USER})(A;;0x1200a9;;;WD)"),         # Everyone can read
    sddl(dacl=f"D:AI(A;;FA;;;{USER})(A;ID;0x1301bf;;;AU)"),        # Authenticated Users can modify (your C:\\dev default)
    sddl(dacl=f"D:AI(A;;FA;;;{USER})(A;ID;0x1200a9;;;BU)"),        # Users group
    sddl(dacl=f"D:P(A;;FA;;;{USER})(A;;FR;;;{OTHER})"),            # another specific account
    sddl(dacl=f"D:P(A;;FA;;;{USER})(XA;;FA;;;WD;(@User.x==1))"),   # conditional allow to Everyone
    sddl(owner=OTHER),                                             # owned by someone else
    f"O:{USER}",                                                   # no access list at all = everyone
    sddl(dacl=f"D:P(A;;FA;;;{USER})(A;;FA;;;CO)"),                 # unknown alias fails closed
    sddl(dacl=f"D:P(A;;FA;;;{USER})(A;;FA)"),                      # malformed entry fails closed
    "",
])
def test_descriptors_with_outside_access_are_not_private(text):
    assert platform_io.sddl_is_private(text, USER) is False


def test_windows_make_private_restricts_then_verifies(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(platform_io, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_io, "_windows_user_sid", lambda: USER)
    monkeypatch.setattr(platform_io, "_windows_restrict_to_user", lambda path: calls.append(("restrict", str(path))))
    monkeypatch.setattr(platform_io, "_windows_sddl", lambda path: calls.append(("read", str(path))) or sddl())
    platform_io.make_private(tmp_path / "secret")
    assert calls == [("restrict", str(tmp_path / "secret")), ("read", str(tmp_path / "secret"))]


def test_windows_make_private_raises_if_acl_is_still_open(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_io, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_io, "_windows_user_sid", lambda: USER)
    monkeypatch.setattr(platform_io, "_windows_restrict_to_user", lambda path: None)
    monkeypatch.setattr(platform_io, "_windows_sddl", lambda path: sddl(dacl=f"D:AI(A;;FA;;;{USER})(A;ID;FA;;;AU)"))
    with pytest.raises(PermissionError):
        platform_io.make_private(tmp_path / "secret")


def test_windows_is_private_is_false_when_the_acl_cannot_be_read(monkeypatch, tmp_path):
    def boom(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(platform_io, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_io, "_windows_user_sid", lambda: USER)
    monkeypatch.setattr(platform_io, "_windows_sddl", boom)
    assert platform_io.is_private_file(tmp_path / "missing") is False


def test_windows_restrict_clears_explicit_entries_then_grants_only_the_user(monkeypatch, tmp_path):
    commands = []
    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
    monkeypatch.setattr(platform_io, "_windows_user_sid", lambda: USER)
    monkeypatch.setattr(platform_io.subprocess, "run", fake_run)
    platform_io._windows_restrict_to_user(tmp_path / "secret")
    target = str(tmp_path / "secret")
    assert all(c[0].lower().endswith("icacls.exe") for c in commands)
    assert [c[1:] for c in commands] == [
        [target, "/reset"],
        [target, "/inheritance:r", "/grant:r", f"*{USER}:F"],
    ]


def test_windows_restrict_failure_is_an_error_not_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_io, "_windows_user_sid", lambda: USER)
    monkeypatch.setattr(platform_io.subprocess, "run",
                        lambda c, **k: subprocess.CompletedProcess(c, 5, stdout="Access is denied.", stderr=""))
    with pytest.raises(PermissionError, match="Access is denied"):
        platform_io._windows_restrict_to_user(tmp_path / "secret")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_posix_make_private_and_is_private_use_mode_600(tmp_path):
    path = tmp_path / "f"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o644)
    assert platform_io.is_private_file(path) is False
    platform_io.make_private(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert platform_io.is_private_file(path) is True
    fd = os.open(path, os.O_RDONLY)
    try:
        assert platform_io.is_private_file(path, fd) is True
    finally:
        os.close(fd)


def test_is_private_file_is_false_for_a_missing_file(tmp_path):
    assert platform_io.is_private_file(tmp_path / "nope") is False


# ---- real Windows check (skipped elsewhere): the part mocks cannot prove -----

@pytest.mark.skipif(os.name != "nt", reason="uses the real Windows ACL APIs and icacls")
def test_real_windows_acl_roundtrip(tmp_path):
    path = tmp_path / "secret.json"
    path.write_text("{}", encoding="utf-8")
    platform_io.make_private(path)
    assert platform_io.is_private_file(path) is True
    # Grant Everyone read access: the policy must now reject the file.
    icacls = platform_io._system32("icacls.exe")
    subprocess.run([icacls, str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    assert platform_io.is_private_file(path) is False
    # Re-restricting repairs it.
    platform_io.make_private(path)
    assert platform_io.is_private_file(path) is True
