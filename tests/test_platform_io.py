"""Offline tests for the OS-difference helpers (Windows branches simulated on any OS)."""
import os
import stat
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


def test_set_private_mode_is_a_noop_when_fchmod_is_missing(monkeypatch, tmp_path):
    monkeypatch.delattr(platform_io.os, "fchmod", raising=False)
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_RDWR)
    try:
        platform_io.set_private_mode(fd)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_set_private_mode_makes_file_owner_only_on_posix(tmp_path):
    fd = os.open(tmp_path / "f", os.O_CREAT | os.O_RDWR, 0o666)
    try:
        platform_io.set_private_mode(fd)
        assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
    finally:
        os.close(fd)
