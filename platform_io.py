"""Small OS-difference helpers for durable file writes (standard library only).

Each helper does the strict POSIX behaviour where the OS supports it and a
documented, narrower behaviour on Windows. Nothing here weakens POSIX behaviour.
"""
from __future__ import annotations

import csv
import functools
import os
import re
import stat
import subprocess
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


# --------------------------------------------------------------------------
# Owner-only ("private") files
#
# POSIX: mode 0o600 and owned by the current user.
# Windows: mode bits do not exist, so privacy means the file's access-control
# list (ACL) grants access to nobody except the current user, SYSTEM and the
# Administrators group. Anything unrecognised is treated as NOT private.
# --------------------------------------------------------------------------

_SYSTEM_SID = "S-1-5-18"
_ADMINS_SID = "S-1-5-32-544"
_SDDL_ALIASES = {"SY": _SYSTEM_SID, "BA": _ADMINS_SID}


def _sddl_sid(token: str) -> str:
    return _SDDL_ALIASES.get(token, token)


def sddl_is_private(sddl: str, user_sid: str) -> bool:
    """Decide from a Windows security descriptor (SDDL text) if a file is owner-only.

    Pure function so the policy is testable on any OS. Private means:
      * the file has an access list (no list at all would mean "everyone");
      * its owner is the user, SYSTEM or Administrators;
      * no "allow" entry names anyone except the user, SYSTEM or Administrators.
    Deny entries are ignored (they only remove access). Inherit-only entries do
    not apply to the file itself. Any trustee that is not a plain SID or one of
    the aliases above (Everyone "WD", Authenticated Users "AU", Users "BU", ...)
    counts as outside access, so unknown input fails closed.
    """
    allowed = {user_sid, _SYSTEM_SID, _ADMINS_SID}
    owner = re.search(r"O:([^:()]+?)(?=[GDS]:|$)", sddl)
    if owner is None or _sddl_sid(owner.group(1)) not in allowed:
        return False
    dacl = re.search(r"D:((?:[A-Z]*)(?:\([^)]*\))*)", sddl)
    if dacl is None:
        return False  # NULL access list: everyone has full access
    for entry in re.findall(r"\(([^)]*)\)", dacl.group(1)):
        fields = entry.split(";")
        if len(fields) < 6:
            return False
        ace_type, flags, trustee = fields[0], fields[1], fields[5]
        if not ace_type.endswith("A") or ace_type in {"AU", "AL"}:
            continue  # not an allow entry (deny, audit, alarm, ...)
        if "IO" in flags:
            continue
        if _sddl_sid(trustee) not in allowed:
            return False
    return True


def _system32(name: str) -> str:
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", name)


@functools.lru_cache(maxsize=1)
def _windows_user_sid() -> str:
    result = subprocess.run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"],
                            capture_output=True, text=True, check=False)
    rows = list(csv.reader(result.stdout.splitlines()))
    if result.returncode != 0 or not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-1-"):
        raise OSError("could not determine the current Windows user SID")
    return rows[0][1]


def _windows_sddl(path) -> str:
    """Owner + access list of a file as SDDL text (Windows only)."""
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    se_file_object, owner_and_dacl = 1, 1 | 4
    get = advapi32.GetNamedSecurityInfoW
    get.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p)]
    get.restype = wintypes.DWORD
    descriptor = ctypes.c_void_p()
    status = get(str(path), se_file_object, owner_and_dacl, None, None, None, None,
                 ctypes.byref(descriptor))
    if status != 0:
        raise ctypes.WinError(status)
    try:
        convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p]
        convert.restype = wintypes.BOOL
        text = wintypes.LPWSTR()
        if not convert(descriptor, 1, owner_and_dacl, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.LocalFree(descriptor)


def _run_icacls(path, *arguments) -> None:
    result = subprocess.run([_system32("icacls.exe"), str(path), *arguments],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PermissionError(f"could not restrict access to {path}: {result.stdout.strip()} {result.stderr.strip()}")


def _windows_restrict_to_user(path) -> None:
    """Leave the file with exactly one access entry: full control for the current user.

    "/grant:r" only replaces entries for the user it names, so a file that already
    carries an explicit entry for someone else (for example Everyone) would keep it.
    "/reset" first clears every explicit entry; "/inheritance:r" then removes the
    inherited ones. A brand-new file only has inherited entries, so for the normal
    case (restricting a file we just created, before writing secrets) this is the
    same end result as before.
    """
    _run_icacls(path, "/reset")
    _run_icacls(path, "/inheritance:r", "/grant:r", f"*{_windows_user_sid()}:F")


def make_private(path, descriptor: int | None = None) -> None:
    """Make a file we just created owner-only. Raises if that cannot be guaranteed.

    POSIX: fchmod/chmod 0o600. Windows: owner-only ACL, then re-read the ACL and
    refuse to continue if it is not private. Call it BEFORE writing secrets so
    nothing sensitive ever sits in a file others could read.
    """
    if IS_WINDOWS:
        _windows_restrict_to_user(path)
        # Read the ACL here (not via is_private_file) so a failure says WHY:
        # either the read itself failed, or this is the descriptor that was judged.
        user_sid = _windows_user_sid()
        text = _windows_sddl(path)
        if not sddl_is_private(text, user_sid):
            raise PermissionError(
                f"access list on {path} is not owner-only after restricting it; "
                f"user={user_sid} descriptor={text}")
        return
    if descriptor is not None:
        os.fchmod(descriptor, 0o600)
    else:
        os.chmod(path, 0o600)


def is_private_file(path, descriptor: int | None = None) -> bool:
    """True only if the file is owner-only (see module notes). Never raises for a missing file."""
    try:
        if IS_WINDOWS:
            return sddl_is_private(_windows_sddl(path), _windows_user_sid())
        metadata = os.fstat(descriptor) if descriptor is not None else os.stat(path)
        return stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_uid == os.getuid()
    except OSError:
        return False
