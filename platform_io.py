"""Small OS-difference helpers for durable file writes (standard library only).

Each helper does the strict POSIX behaviour where the OS supports it and a
documented, narrower behaviour on Windows. Nothing here weakens POSIX behaviour.
"""
from __future__ import annotations

import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# 0 on Windows, where the flag does not exist. Pair with refuse_symlink().
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def fsync_directory(path) -> None:
    """Flush a directory entry (a rename or create) to disk.

    POSIX: open the directory and fsync it.
    Windows: a directory cannot be opened like a file (PermissionError), so this
    step is skipped. File contents are still fsynced by the caller and
    os.replace stays atomic; only "rename survives a power cut within moments
    of the save" is no longer guaranteed. Revisit before unattended submission.
    """
    if IS_WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def refuse_symlink(path) -> None:
    """Raise OSError if ``path`` is a symlink or directory junction.

    Replaces O_NOFOLLOW on Windows, where that flag does not exist. It is also
    safe on POSIX, but there O_NOFOLLOW on the open itself remains the real guard
    (this check alone has a small check-then-open gap).
    """
    p = Path(path)
    is_junction = getattr(os.path, "isjunction", lambda _p: False)
    if p.is_symlink() or is_junction(p):
        raise OSError(f"refusing to follow link: {p}")


def set_private_mode(descriptor: int) -> None:
    """Best-effort chmod 0o600 on an open file. No-op where fchmod is missing.

    Windows ignores POSIX mode bits, so this does NOT make the file private
    there; privacy on Windows comes from where the file lives (under the user's
    profile) and, later, from ACL checks.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is not None:
        fchmod(descriptor, 0o600)
