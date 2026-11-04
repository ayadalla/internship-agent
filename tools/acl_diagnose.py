"""Windows-only diagnostic: show what the owner-only ACL check sees. Prints no secrets.

    .\\.venv\\Scripts\\python.exe tools\\acl_diagnose.py
"""
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_io

if os.name != "nt":
    sys.exit("run this on Windows")

with tempfile.TemporaryDirectory() as directory:
    path = Path(directory) / "probe.json"
    path.write_text("{}", encoding="utf-8")
    for label, step in (
        ("user SID (whoami)", platform_io._windows_user_sid),
        ("restrict (icacls)", lambda: platform_io._windows_restrict_to_user(path)),
        ("descriptor (ctypes)", lambda: platform_io._windows_sddl(path)),
    ):
        try:
            print(f"{label}: {step()!r}")
        except Exception:
            print(f"{label}: FAILED")
            traceback.print_exc()
    print("icacls shows:")
    print(subprocess.run([platform_io._system32("icacls.exe"), str(path)],
                         capture_output=True, text=True).stdout)
    try:
        sddl = platform_io._windows_sddl(path)
        print("policy says private:", platform_io.sddl_is_private(sddl, platform_io._windows_user_sid()))
    except Exception:
        print("policy check: FAILED")
        traceback.print_exc()
