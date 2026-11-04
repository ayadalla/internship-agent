"""Cross-platform non-blocking exclusive file lock (standard library only).

POSIX uses ``fcntl.flock``. Windows has no ``fcntl``, so it uses
``msvcrt.locking`` on the first byte of the file. Both behave the same way for
this project's purpose: one holder at a time, never wait, and the lock is
released when the descriptor is closed or the process dies.

Windows locks are mandatory and per handle. A second handle to the same file,
even inside the same process, is refused while the first holds the lock, which
matches ``flock`` semantics for separately opened descriptors.
"""
from __future__ import annotations

import errno
import os

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    import msvcrt

    fcntl = None
else:
    import fcntl

    msvcrt = None

# msvcrt.locking reports "someone else holds it" as EACCES (non-blocking mode)
# or EDEADLK (EDEADLOCK on Windows) after its internal retries. Any other
# OSError is a real fault and must not be mistaken for "lock is busy".
_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}


def try_lock(fd: int) -> bool:
    """Try to take an exclusive lock on an open descriptor without waiting.

    Returns True if the lock was acquired and False if another holder has it.
    Unexpected errors are raised, never reported as "busy".
    """
    if _IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _BUSY_ERRNOS:
                return False
            raise
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def unlock(fd: int) -> None:
    """Release a lock taken with try_lock. Closing the descriptor also releases it."""
    if _IS_WINDOWS:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
