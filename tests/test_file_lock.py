"""Offline tests for the cross-platform file lock helper."""
import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import file_lock


def _open_two(path):
    first = os.open(path, os.O_CREAT | os.O_RDWR)
    second = os.open(path, os.O_CREAT | os.O_RDWR)
    return first, second


def test_second_descriptor_is_refused_while_first_holds_the_lock(tmp_path):
    """Real locking on the running OS: one holder at a time, never waits."""
    path = tmp_path / "held.lock"
    first, second = _open_two(path)
    try:
        assert file_lock.try_lock(first) is True
        assert file_lock.try_lock(second) is False
    finally:
        os.close(first)
        os.close(second)


def test_lock_can_be_taken_again_after_unlock(tmp_path):
    path = tmp_path / "released.lock"
    first, second = _open_two(path)
    try:
        assert file_lock.try_lock(first) is True
        file_lock.unlock(first)
        assert file_lock.try_lock(second) is True
    finally:
        os.close(first)
        os.close(second)


def test_lock_is_released_when_the_holder_closes_its_descriptor(tmp_path):
    path = tmp_path / "closed.lock"
    first, second = _open_two(path)
    try:
        assert file_lock.try_lock(first) is True
        os.close(first)
        assert file_lock.try_lock(second) is True
    finally:
        os.close(second)


class FakeMsvcrt:
    """Stand-in for Windows' msvcrt so the Windows branch is exercised on any OS."""

    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if self.error is not None:
            raise self.error


@pytest.fixture
def windows_branch(monkeypatch):
    def install(fake):
        monkeypatch.setattr(file_lock, "_IS_WINDOWS", True)
        monkeypatch.setattr(file_lock, "msvcrt", fake)

    return install


def test_windows_branch_locks_first_byte_from_start_of_file(tmp_path, windows_branch):
    fake = FakeMsvcrt()
    windows_branch(fake)
    fd = os.open(tmp_path / "w.lock", os.O_CREAT | os.O_RDWR)
    try:
        os.write(fd, b"existing")  # leaves the file position at the end
        assert file_lock.try_lock(fd) is True
    finally:
        os.close(fd)
    assert fake.calls == [(FakeMsvcrt.LK_NBLCK, 1, 0)]


@pytest.mark.parametrize("code", [errno.EACCES, errno.EDEADLK])
def test_windows_branch_reports_busy_for_lock_conflict_errors(tmp_path, windows_branch, code):
    windows_branch(FakeMsvcrt(error=OSError(code, "locked")))
    fd = os.open(tmp_path / "busy.lock", os.O_CREAT | os.O_RDWR)
    try:
        assert file_lock.try_lock(fd) is False
    finally:
        os.close(fd)


def test_windows_branch_does_not_hide_unrelated_errors(tmp_path, windows_branch):
    windows_branch(FakeMsvcrt(error=OSError(errno.EBADF, "bad descriptor")))
    fd = os.open(tmp_path / "bad.lock", os.O_CREAT | os.O_RDWR)
    try:
        with pytest.raises(OSError) as raised:
            file_lock.try_lock(fd)
    finally:
        os.close(fd)
    assert raised.value.errno == errno.EBADF


def test_windows_branch_unlock_rewinds_then_releases(tmp_path, windows_branch):
    fake = FakeMsvcrt()
    windows_branch(fake)
    fd = os.open(tmp_path / "u.lock", os.O_CREAT | os.O_RDWR)
    try:
        os.write(fd, b"abc")
        file_lock.unlock(fd)
    finally:
        os.close(fd)
    assert fake.calls == [(FakeMsvcrt.LK_UNLCK, 1, 0)]
