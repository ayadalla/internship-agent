from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sources


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_greenhouse_adapter_returns_postings_for_downstream_profile_classification():
    calls: list[str] = []

    def fake_open(url: str, timeout: float):
        calls.append(url)
        assert timeout == 15.0
        return FakeResponse(
            {
                "jobs": [
                    {
                        "id": 101,
                        "title": "Software Engineer Intern",
                        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/101",
                        "location": {"name": "San Francisco, CA"},
                        "updated_at": "2026-08-23T18:00:00Z",
                    },
                    {
                        "id": 102,
                        "title": "Senior Software Engineer",
                        "absolute_url": "https://job-boards.greenhouse.io/example/jobs/102",
                        "location": {"name": "Remote"},
                        "updated_at": "2026-08-23T18:00:00Z",
                    },
                ]
            }
        )

    jobs = sources.fetch_greenhouse_jobs("example", opener=fake_open)

    assert calls == ["https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"]
    assert jobs == [
        {
            "company": "Example",
            "role": "Software Engineer Intern",
            "url": "https://job-boards.greenhouse.io/example/jobs/101",
            "location": "San Francisco, CA",
            "source": "Greenhouse public API",
            "updated_at": "2026-08-23T18:00:00Z",
        },
        {
            "company": "Example", "role": "Senior Software Engineer",
            "url": "https://job-boards.greenhouse.io/example/jobs/102",
            "location": "Remote", "source": "Greenhouse public API",
            "updated_at": "2026-08-23T18:00:00Z",
        },
    ]



def test_lever_adapter_retries_transient_failures_without_an_intern_only_filter():
    attempts = {"count": 0}

    def flaky_open(url: str, timeout: float):
        attempts["count"] += 1
        assert url == "https://api.lever.co/v0/postings/example?mode=json"
        assert timeout == 15.0
        if attempts["count"] < 3:
            raise TimeoutError("temporary network issue")
        return FakeResponse(
            [
                {
                    "text": "Data Scientist Intern",
                    "hostedUrl": "https://jobs.lever.co/example/1",
                    "categories": {"location": "San Diego, CA", "team": "Data"},
                    "createdAt": 1787517600000,
                },
                {
                    "text": "Senior Data Scientist",
                    "hostedUrl": "https://jobs.lever.co/example/2",
                    "categories": {"location": "Remote", "team": "Data"},
                    "createdAt": 1787517600000,
                },
            ]
        )

    jobs = sources.fetch_lever_jobs("example", opener=flaky_open, attempts=3)

    assert attempts["count"] == 3
    assert jobs == [
        {
            "company": "Example",
            "role": "Data Scientist Intern",
            "url": "https://jobs.lever.co/example/1",
            "location": "San Diego, CA",
            "team": "Data",
            "source": "Lever public API",
            "created_at": 1787517600000,
        },
        {
            "company": "Example", "role": "Senior Data Scientist",
            "url": "https://jobs.lever.co/example/2", "location": "Remote",
            "team": "Data", "source": "Lever public API", "created_at": 1787517600000,
        },
    ]



def test_main_combines_source_tokens_deduplicates_urls_and_writes_candidates_json(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1?gh_src=test",
                "location": "Remote",
                "source": "Greenhouse public API",
            }
        ]

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "lever-co"
        return [
            {
                "company": "Lever Co",
                "role": "Data Scientist Intern",
                "url": "https://jobs.lever.co/lever/2",
                "location": "San Diego, CA",
                "source": "Lever public API",
            },
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1",
                "location": "Remote",
                "source": "Lever public API",
            },
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--lever", "lever-co",
        "--output", str(output),
    ])

    assert exit_code == 3
    assert json.loads(output.read_text(encoding="utf-8")) == [
        {
            "company": "Green Co",
            "role": "Software Engineer Intern",
            "url": "https://job-boards.greenhouse.io/green/jobs/1",
            "location": "Remote",
            "source": "Greenhouse public API",
        },
        {
            "company": "Lever Co",
            "role": "Data Scientist Intern",
            "url": "https://jobs.lever.co/lever/2",
            "location": "San Diego, CA",
            "source": "Lever public API",
        },
    ]
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co"],
        "lever_tokens": ["lever-co"],
        "candidates": 2,
        "failures": [],
        "source_health_status": "stale_or_unknown",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 1,
                "freshness_unknown": True,
                "warning": "No posting timestamps available; freshness unknown",
            },
            {
                "source": "lever",
                "token": "lever-co",
                "status": "ok",
                "candidates": 2,
                "freshness_unknown": True,
                "warning": "No posting timestamps available; freshness unknown",
            },
        ],
        "freshness_summary": {
            "total_runs": 2,
            "healthy_runs": 0,
            "stale_runs": 0,
            "freshness_unknown_runs": 2,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [],
            "stale": [],
            "freshness_unknown": [
                {"source": "greenhouse", "token": "green-co"},
                {"source": "lever", "token": "lever-co"},
            ],
            "error": [],
        },
        "output": str(output),
        "freshness_unknown": True,
        "warning": "One or more configured source runs succeeded without posting timestamps; freshness unknown",
        "stale_result": True,
    }



def test_main_writes_report_artifact_matching_stdout_payload(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"
    report = tmp_path / "sources-report.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1?gh_src=test",
                "location": "Remote",
                "source": "Greenhouse public API",
            }
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--output", str(output),
        "--report", str(report),
    ])

    stdout_payload = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert json.loads(output.read_text(encoding="utf-8")) == [
        {
            "company": "Green Co",
            "role": "Software Engineer Intern",
            "url": "https://job-boards.greenhouse.io/green/jobs/1",
            "location": "Remote",
            "source": "Greenhouse public API",
        }
    ]
    assert json.loads(report.read_text(encoding="utf-8")) == stdout_payload
    assert stdout_payload["output"] == str(output)
    assert stdout_payload["report"] == str(report)
    assert stdout_payload["source_health_status"] == "stale_or_unknown"



def test_main_reports_failed_tokens_without_losing_successful_candidates(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "broken-co":
            raise RuntimeError("Failed to fetch greenhouse token")
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1?utm_source=test",
                "location": "Remote",
                "source": "Greenhouse public API",
            }
        ]

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "lever-co"
        return [
            {
                "company": "Lever Co",
                "role": "Data Scientist Intern",
                "url": "https://jobs.lever.co/lever/2",
                "location": "San Diego, CA",
                "source": "Lever public API",
            }
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--greenhouse", "broken-co",
        "--lever", "lever-co",
        "--output", str(output),
    ])

    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8")) == [
        {
            "company": "Green Co",
            "role": "Software Engineer Intern",
            "url": "https://job-boards.greenhouse.io/green/jobs/1",
            "location": "Remote",
            "source": "Greenhouse public API",
        },
        {
            "company": "Lever Co",
            "role": "Data Scientist Intern",
            "url": "https://jobs.lever.co/lever/2",
            "location": "San Diego, CA",
            "source": "Lever public API",
        },
    ]
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co", "broken-co"],
        "lever_tokens": ["lever-co"],
        "candidates": 2,
        "failures": [
            {
                "source": "greenhouse",
                "token": "broken-co",
                "error": "Failed to fetch greenhouse token",
            }
        ],
        "source_health_status": "partial_error",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 1,
                "freshness_unknown": True,
                "warning": "No posting timestamps available; freshness unknown",
            },
            {
                "source": "greenhouse",
                "token": "broken-co",
                "status": "error",
                "error": "Failed to fetch greenhouse token",
                "candidates": 0,
            },
            {
                "source": "lever",
                "token": "lever-co",
                "status": "ok",
                "candidates": 1,
                "freshness_unknown": True,
                "warning": "No posting timestamps available; freshness unknown",
            },
        ],
        "freshness_summary": {
            "total_runs": 3,
            "healthy_runs": 0,
            "stale_runs": 0,
            "freshness_unknown_runs": 2,
            "error_runs": 1,
        },
        "freshness_buckets": {
            "healthy": [],
            "stale": [],
            "freshness_unknown": [
                {"source": "greenhouse", "token": "green-co"},
                {"source": "lever", "token": "lever-co"},
            ],
            "error": [
                {"source": "greenhouse", "token": "broken-co"},
            ],
        },
        "output": str(output),
    }



def test_main_signals_zero_candidates_when_sources_return_no_internships(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return []

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "lever-co"
        return []

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--lever", "lever-co",
        "--output", str(output),
    ])

    assert exit_code == 3
    assert json.loads(output.read_text(encoding="utf-8")) == []
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co"],
        "lever_tokens": ["lever-co"],
        "candidates": 0,
        "failures": [],
        "source_health_status": "stale_or_unknown",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 0,
            },
            {
                "source": "lever",
                "token": "lever-co",
                "status": "ok",
                "candidates": 0,
            },
        ],
        "freshness_summary": {
            "total_runs": 2,
            "healthy_runs": 2,
            "stale_runs": 0,
            "freshness_unknown_runs": 0,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [
                {"source": "greenhouse", "token": "green-co"},
                {"source": "lever", "token": "lever-co"},
            ],
            "stale": [],
            "freshness_unknown": [],
            "error": [],
        },
        "output": str(output),
        "warning": "Configured source tokens returned zero job postings",
        "stale_result": True,
    }



def test_main_signals_stale_non_empty_results_when_newest_posting_is_old(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1",
                "location": "Remote",
                "source": "Greenhouse public API",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", lambda *args, **kwargs: [])
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc), raising=False)

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--output", str(output),
    ])

    assert exit_code == 3
    assert json.loads(output.read_text(encoding="utf-8")) == [
        {
            "company": "Green Co",
            "role": "Software Engineer Intern",
            "url": "https://job-boards.greenhouse.io/green/jobs/1",
            "location": "Remote",
            "source": "Greenhouse public API",
            "updated_at": "2026-06-01T00:00:00Z",
        }
    ]
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co"],
        "lever_tokens": [],
        "candidates": 1,
        "failures": [],
        "source_health_status": "stale_or_unknown",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 1,
                "latest_posting_at": "2026-06-01T00:00:00Z",
                "stale_result": True,
                "warning": "Newest posting timestamp is older than 30 days",
            }
        ],
        "freshness_summary": {
            "total_runs": 1,
            "healthy_runs": 0,
            "stale_runs": 1,
            "freshness_unknown_runs": 0,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [],
            "stale": [
                {"source": "greenhouse", "token": "green-co"},
            ],
            "freshness_unknown": [],
            "error": [],
        },
        "output": str(output),
        "warning": "Newest posting timestamp is older than 30 days",
        "stale_result": True,
        "latest_posting_at": "2026-06-01T00:00:00Z",
    }



def test_main_reports_per_token_freshness_when_one_source_is_stale(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1",
                "location": "Remote",
                "source": "Greenhouse public API",
                "updated_at": "2026-06-01T00:00:00Z",
            }
        ]

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "lever-co"
        return [
            {
                "company": "Lever Co",
                "role": "Data Scientist Intern",
                "url": "https://jobs.lever.co/lever/2",
                "location": "Remote",
                "source": "Lever public API",
                "created_at": 1787443200000,
            }
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc), raising=False)

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--lever", "lever-co",
        "--output", str(output),
    ])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co"],
        "lever_tokens": ["lever-co"],
        "candidates": 2,
        "failures": [],
        "source_health_status": "stale_or_unknown",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 1,
                "latest_posting_at": "2026-06-01T00:00:00Z",
                "stale_result": True,
                "warning": "Newest posting timestamp is older than 30 days",
            },
            {
                "source": "lever",
                "token": "lever-co",
                "status": "ok",
                "candidates": 1,
                "latest_posting_at": "2026-08-23T00:00:00Z",
            },
        ],
        "freshness_summary": {
            "total_runs": 2,
            "healthy_runs": 1,
            "stale_runs": 1,
            "freshness_unknown_runs": 0,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [
                {"source": "lever", "token": "lever-co"},
            ],
            "stale": [
                {"source": "greenhouse", "token": "green-co"},
            ],
            "freshness_unknown": [],
            "error": [],
        },
        "output": str(output),
        "latest_posting_at": "2026-08-23T00:00:00Z",
    }



def test_main_marks_source_run_when_posting_timestamps_are_missing(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1",
                "location": "Remote",
                "source": "Greenhouse public API",
            }
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", lambda *args, **kwargs: [])

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--output", str(output),
    ])

    assert exit_code == 3
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co"],
        "lever_tokens": [],
        "candidates": 1,
        "failures": [],
        "source_health_status": "stale_or_unknown",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 1,
                "freshness_unknown": True,
                "warning": "No posting timestamps available; freshness unknown",
            }
        ],
        "freshness_summary": {
            "total_runs": 1,
            "healthy_runs": 0,
            "stale_runs": 0,
            "freshness_unknown_runs": 1,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [],
            "stale": [],
            "freshness_unknown": [
                {"source": "greenhouse", "token": "green-co"},
            ],
            "error": [],
        },
        "output": str(output),
        "freshness_unknown": True,
        "warning": "One or more configured source runs succeeded without posting timestamps; freshness unknown",
        "stale_result": True,
    }



def test_main_fails_closed_when_any_source_run_is_missing_timestamps(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        assert token == "green-co"
        return [
            {
                "company": "Green Co",
                "role": "Software Engineer Intern",
                "url": "https://job-boards.greenhouse.io/green/jobs/1",
                "location": "Remote",
                "source": "Greenhouse public API",
            }
        ]

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", lambda *args, **kwargs: [])

    exit_code = sources.main([
        "--greenhouse", "green-co",
        "--output", str(output),
    ])

    assert exit_code == 3
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": ["green-co"],
        "lever_tokens": [],
        "candidates": 1,
        "failures": [],
        "source_health_status": "stale_or_unknown",
        "source_runs": [
            {
                "source": "greenhouse",
                "token": "green-co",
                "status": "ok",
                "candidates": 1,
                "freshness_unknown": True,
                "warning": "No posting timestamps available; freshness unknown",
            }
        ],
        "freshness_summary": {
            "total_runs": 1,
            "healthy_runs": 0,
            "stale_runs": 0,
            "freshness_unknown_runs": 1,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [],
            "stale": [],
            "freshness_unknown": [
                {"source": "greenhouse", "token": "green-co"},
            ],
            "error": [],
        },
        "output": str(output),
        "freshness_unknown": True,
        "warning": "One or more configured source runs succeeded without posting timestamps; freshness unknown",
        "stale_result": True,
    }



def test_main_reports_aggregate_freshness_summary_counts(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "stale-co":
            return [
                {
                    "company": "Stale Co",
                    "role": "Software Engineer Intern",
                    "url": "https://job-boards.greenhouse.io/stale/jobs/1",
                    "location": "Remote",
                    "source": "Greenhouse public API",
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ]
        if token == "unknown-co":
            return [
                {
                    "company": "Unknown Co",
                    "role": "ML Intern",
                    "url": "https://job-boards.greenhouse.io/unknown/jobs/2",
                    "location": "Remote",
                    "source": "Greenhouse public API",
                }
            ]
        raise RuntimeError(f"unexpected greenhouse token: {token}")

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "fresh-co":
            return [
                {
                    "company": "Fresh Co",
                    "role": "Data Intern",
                    "url": "https://jobs.lever.co/fresh/3",
                    "location": "Remote",
                    "source": "Lever public API",
                    "created_at": 1787443200000,
                }
            ]
        if token == "empty-co":
            return []
        raise RuntimeError(f"unexpected lever token: {token}")

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc), raising=False)

    exit_code = sources.main([
        "--greenhouse", "stale-co",
        "--greenhouse", "unknown-co",
        "--lever", "fresh-co",
        "--lever", "empty-co",
        "--output", str(output),
    ])

    assert exit_code == 3
    result = json.loads(capsys.readouterr().out)
    assert result["freshness_summary"] == {
        "total_runs": 4,
        "healthy_runs": 2,
        "stale_runs": 1,
        "freshness_unknown_runs": 1,
        "error_runs": 0,
    }



def test_main_reports_freshness_bucket_tokens_for_each_source_status(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "stale-co":
            return [
                {
                    "company": "Stale Co",
                    "role": "Software Engineer Intern",
                    "url": "https://job-boards.greenhouse.io/stale/jobs/1",
                    "location": "Remote",
                    "source": "Greenhouse public API",
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ]
        if token == "unknown-co":
            return [
                {
                    "company": "Unknown Co",
                    "role": "ML Intern",
                    "url": "https://job-boards.greenhouse.io/unknown/jobs/2",
                    "location": "Remote",
                    "source": "Greenhouse public API",
                }
            ]
        raise RuntimeError("broken greenhouse token")

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "fresh-co":
            return [
                {
                    "company": "Fresh Co",
                    "role": "Data Intern",
                    "url": "https://jobs.lever.co/fresh/3",
                    "location": "Remote",
                    "source": "Lever public API",
                    "created_at": 1787443200000,
                }
            ]
        if token == "empty-co":
            return []
        raise RuntimeError(f"unexpected lever token: {token}")

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc), raising=False)

    exit_code = sources.main([
        "--greenhouse", "stale-co",
        "--greenhouse", "unknown-co",
        "--greenhouse", "error-co",
        "--lever", "fresh-co",
        "--lever", "empty-co",
        "--output", str(output),
    ])

    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["freshness_buckets"] == {
        "healthy": [
            {"source": "lever", "token": "empty-co"},
            {"source": "lever", "token": "fresh-co"},
        ],
        "stale": [
            {"source": "greenhouse", "token": "stale-co"},
        ],
        "freshness_unknown": [
            {"source": "greenhouse", "token": "unknown-co"},
        ],
        "error": [
            {"source": "greenhouse", "token": "error-co"},
        ],
    }



def test_main_reports_top_level_source_health_status(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candidates.json"

    def fake_greenhouse(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "stale-co":
            return [
                {
                    "company": "Stale Co",
                    "role": "Software Engineer Intern",
                    "url": "https://job-boards.greenhouse.io/stale/jobs/1",
                    "location": "Remote",
                    "source": "Greenhouse public API",
                    "updated_at": "2026-06-01T00:00:00Z",
                }
            ]
        raise RuntimeError("broken greenhouse token")

    def fake_lever(token: str, *, opener=sources._default_open, attempts=sources.DEFAULT_ATTEMPTS):
        if token == "fresh-co":
            return [
                {
                    "company": "Fresh Co",
                    "role": "Data Intern",
                    "url": "https://jobs.lever.co/fresh/3",
                    "location": "Remote",
                    "source": "Lever public API",
                    "created_at": 1787443200000,
                }
            ]
        raise RuntimeError(f"unexpected lever token: {token}")

    monkeypatch.setattr(sources, "fetch_greenhouse_jobs", fake_greenhouse)
    monkeypatch.setattr(sources, "fetch_lever_jobs", fake_lever)
    monkeypatch.setattr(sources, "_utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc), raising=False)

    exit_code = sources.main([
        "--greenhouse", "stale-co",
        "--greenhouse", "error-co",
        "--lever", "fresh-co",
        "--output", str(output),
    ])

    assert exit_code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["source_health_status"] == "partial_error"



def test_main_requires_at_least_one_source_token(tmp_path, capsys):
    output = tmp_path / "candidates.json"

    exit_code = sources.main([
        "--output", str(output),
    ])

    assert exit_code == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().out) == {
        "greenhouse_tokens": [],
        "lever_tokens": [],
        "candidates": 0,
        "failures": [],
        "source_health_status": "partial_error",
        "freshness_summary": {
            "total_runs": 0,
            "healthy_runs": 0,
            "stale_runs": 0,
            "freshness_unknown_runs": 0,
            "error_runs": 0,
        },
        "freshness_buckets": {
            "healthy": [],
            "stale": [],
            "freshness_unknown": [],
            "error": [],
        },
        "output": str(output),
        "error": "At least one --greenhouse or --lever token is required",
    }
