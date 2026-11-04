"""Test helper: create a symlink, or skip when this OS account is not allowed to.

On Windows, creating symlinks needs Developer Mode or an elevated shell
(WinError 1314). Only that specific refusal becomes a skip; every other error
still fails the test, so real problems are not hidden.
"""
import pytest


def symlink_or_skip(link, target, **kwargs):
    try:
        link.symlink_to(target, **kwargs)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlink creation needs Developer Mode or admin on this Windows account")
        raise
