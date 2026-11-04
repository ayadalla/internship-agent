from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_gitignore_covers_runtime_and_report_artifacts():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "runtime/" in gitignore
    assert "orchestrator-report.json" in gitignore
