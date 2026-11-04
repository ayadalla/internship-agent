from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import kevin_bible_sync


def test_fetch_google_document_enriches_docs_text_with_drive_source_timestamp(monkeypatch):
    responses = iter(
        [
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"documentId": "doc-source", "body": "[answers]\n[/answers]"}),
            ),
            SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"id": "doc-source", "modifiedTime": "2026-08-25T14:00:00Z"}),
            ),
        ]
    )
    monkeypatch.setattr(kevin_bible_sync.subprocess, "run", lambda *args, **kwargs: next(responses))

    document = kevin_bible_sync.fetch_google_document("doc-source")

    assert document["documentId"] == "doc-source"
    assert document["modifiedTime"] == "2026-08-25T14:00:00Z"
    assert document["revisionId"] == "2026-08-25T14:00:00Z"


def test_sync_caches_source_timestamp_and_reports_profile_precedence_without_pii_in_logs(tmp_path):
    cache_path = tmp_path / "runtime" / "kevin-bible-cache.json"
    logs: list[str] = []
    document = {
        "revisionId": "revision-42",
        "modifiedTime": "2026-08-25T09:00:00Z",
        "answers": [
            {
                "question_key": "graduation_season",
                "answer": "Winter 2027",
                "contact": "kevinkpyo@gmail.com",
            }
        ],
    }
    profile = {"education": {"graduation_season": "Spring 2028"}}

    result = kevin_bible_sync.sync(
        doc_id="doc-123",
        profile=profile,
        cache_path=cache_path,
        fetch_document=lambda _: document,
        log=logs.append,
        now="2026-08-25T10:00:00Z",
    )

    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["source"] == {
        "doc_id": "doc-123",
        "revision_id": "revision-42",
        "modified_at": "2026-08-25T09:00:00Z",
        "fetched_at": "2026-08-25T10:00:00Z",
    }
    assert cached["answers"] == document["answers"]
    assert result["conflicts"] == [
        {
            "question_key": "graduation_season",
            "winner": "profile",
            "discarded_source": "kevin_bible",
        }
    ]
    assert all("kevinkpyo@gmail.com" not in entry for entry in logs)


def test_sync_parses_structured_google_doc_body_into_answer_entries(tmp_path):
    cache_path = tmp_path / "runtime" / "kevin-bible-cache.json"
    document = {
        "documentId": "doc-structured",
        "revisionId": "revision-43",
        "modifiedTime": "2026-08-25T11:00:00Z",
        "body": """[answers]
question_key: portfolio_url
answer: https://example.test/portfolio
company: Example Co
---
question_key: graduation_season
answer: Spring 2028
[/answers]
""",
    }

    result = kevin_bible_sync.sync(
        doc_id="doc-structured",
        profile={},
        cache_path=cache_path,
        fetch_document=lambda _: document,
        now="2026-08-25T12:00:00Z",
    )

    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cached["answers"] == [
        {
            "question_key": "portfolio_url",
            "answer": "https://example.test/portfolio",
            "company": "Example Co",
        },
        {"question_key": "graduation_season", "answer": "Spring 2028"},
    ]
    assert result["answer_count"] == 2


def test_main_uses_private_default_cache_and_emits_sanitized_summary(tmp_path, monkeypatch, capsys):
    default_cache = tmp_path / "runtime" / "kevin-bible-cache.json"
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kevin_bible_sync, "DEFAULT_CACHE_PATH", default_cache)
    monkeypatch.setattr(
        kevin_bible_sync,
        "fetch_google_document",
        lambda _: {
            "revisionId": "revision-44",
            "modifiedTime": "2026-08-25T13:00:00Z",
            "answers": [{"question_key": "portfolio_url", "answer": "https://private.example/test"}],
        },
    )

    assert kevin_bible_sync.main(["--doc-id", "doc-cli", "--profile", str(profile_path)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["answer_count"] == 1
    assert result["source"]["doc_id"] == "doc-cli"
    assert default_cache.exists()
    assert "private.example" not in json.dumps(result)
