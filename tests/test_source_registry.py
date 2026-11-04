from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import source_registry
import sources


def test_load_registry_returns_sorted_approved_greenhouse_and_lever_boards(tmp_path):
    registry = tmp_path / "approved-sources.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {"platform": "lever", "token": "zeta", "approved": True},
                    {"platform": "greenhouse", "token": "alpha", "approved": True},
                    {"platform": "lever", "token": "disabled", "approved": False},
                ],
            }
        )
    , encoding="utf-8")

    assert source_registry.load_registry(registry) == {
        "version": 1,
        "sources": [
            {"platform": "greenhouse", "token": "alpha"},
            {"platform": "lever", "token": "zeta"},
        ],
    }


def test_sources_main_collects_only_approved_registry_entries_in_deterministic_order(tmp_path, monkeypatch, capsys):
    registry = tmp_path / "approved-sources.json"
    registry.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": [
                    {"platform": "lever", "token": "zeta", "approved": True},
                    {"platform": "greenhouse", "token": "alpha", "approved": True},
                    {"platform": "greenhouse", "token": "disabled", "approved": False},
                ],
            }
        )
    , encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def fake_greenhouse(token: str, **_kwargs):
        calls.append(("greenhouse", token))
        return []

    def fake_lever(token: str, **_kwargs):
        calls.append(("lever", token))
        return []

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)

    assert sources.main(["--registry", str(registry), "--output", str(tmp_path / "candidates.json")]) == 3
    assert calls == [("greenhouse", "alpha"), ("lever", "zeta")]
    assert json.loads(capsys.readouterr().out)["greenhouse_tokens"] == ["alpha"]
