from __future__ import annotations

import json
from io import StringIO

import pytest
import yaml
from conftest import FIXTURES, read_log

from cron_agents import cli
from cron_agents.cli import _read_sources, main
from cron_agents.db import Database
from cron_agents.jobs import rss


def test_full_local_fixture_run(project, capsys, monkeypatch) -> None:
    project.data["jobs"]["rss"]["feeds"].append(
        {"name": "atom", "url": (FIXTURES / "feed.atom").as_uri()}
    )
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))
    monkeypatch.setattr(rss, "utc_now", lambda: "2026-07-31T12:00:00+00:00")
    assert main(["--config", str(project.config), "run", "rss"]) == 0
    rss_result = json.loads(capsys.readouterr().out)
    assert rss_result == {"fetched": 4, "inserted": 4, "job": "rss"}

    assert main(["--config", str(project.config), "run", "briefing", "--date", "2026-07-31"]) == 0
    briefing_result = json.loads(capsys.readouterr().out)
    assert briefing_result["sources"] == 2
    output = (project.root / "briefings" / "2026-07-31.md").read_text()

    curator, writer = read_log(project.log)
    candidates = json.loads(curator["prompt"].split("CANDIDATE_SOURCES=", 1)[1])
    selected = json.loads(
        (project.root / "data" / "selections" / "2026-07-31.json").read_text()
    )["source_ids"]
    chosen = json.loads(writer["prompt"].split("SELECTED_SOURCES=", 1)[1])
    candidates_by_id = {record["id"]: record for record in candidates}

    assert [curator["kind"], writer["kind"]] == ["curator", "writer"]
    assert {record["title"] for record in candidates} == {
        "First useful release",
        "Second useful release",
        "Rejected candidate",
        "Atom entry",
    }
    assert all(record["content"] for record in candidates)
    assert "Briefing context: Fixture briefing." in curator["prompt"]
    assert "Briefing context: Fixture briefing." in writer["prompt"]
    assert chosen == [candidates_by_id[source_id] for source_id in selected]
    assert "Rejected candidate" not in {record["title"] for record in chosen}
    assert all(f'  - "{source_id}"' in output for source_id in selected)
    assert output.count("[Source](") == len(selected)


def test_rss_cli_saves_healthy_feed_and_reports_broken_feed(
    project, capsys, monkeypatch
) -> None:
    broken = project.root / "broken.xml"
    broken.write_bytes(b'<?xml version="1.0" encoding="unknown"?><rss/>')
    project.data["jobs"]["rss"]["feeds"].append(
        {"name": "broken", "url": broken.as_uri()}
    )
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))
    monkeypatch.setattr(rss, "utc_now", lambda: "2026-07-31T12:00:00+00:00")

    assert main(["--config", str(project.config), "run", "rss"]) == 1

    captured = capsys.readouterr()
    database = Database(project.root / "data" / "state.db")
    saved = database.available_sources(
        since="",
        before="9999-12-31T23:59:59+00:00",
        excluded_ids=set(),
        limit=10,
    )
    assert {item.title for item in saved} == {
        "First useful release",
        "Second useful release",
        "Rejected candidate",
    }
    assert "broken: invalid RSS or Atom XML" in captured.err
    assert captured.out == ""


def test_unknown_job_returns_clear_error(project, capsys) -> None:
    assert main(["--config", str(project.config), "run", "missing"]) == 1

    captured = capsys.readouterr()
    assert "unknown job 'missing'" in captured.err
    assert captured.out == ""


def test_malformed_yaml_returns_clear_error(project, capsys) -> None:
    project.config.write_text("models: [")

    assert main(["--config", str(project.config), "run", "rss"]) == 1

    captured = capsys.readouterr()
    assert captured.err.startswith("error: invalid YAML")
    assert "Traceback" not in captured.err


def test_imports_jsonl_as_fetched_sources(project, tmp_path, capsys) -> None:
    path = tmp_path / "sources.jsonl"
    path.write_text(
        json.dumps(
            {
                "provider": "x",
                "provider_id": "123",
                "url": "https://x.com/example/status/123",
                "title": "A useful post",
                "content": "Concrete details from the post.",
                "author": "example",
            }
        )
        + "\n"
    )

    assert main(["--config", str(project.config), "import", str(path)]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "inserted": 1,
        "job": "import",
        "published": False,
        "records": 1,
    }


def test_imports_history_as_published_and_updates_existing_url(project, tmp_path, capsys) -> None:
    existing = tmp_path / "existing.jsonl"
    existing.write_text(
        json.dumps(
            {
                "provider": "rss",
                "provider_id": "current",
                "url": "https://example.com/seen",
                "title": "Current source",
            }
        )
        + "\n"
    )
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "provider": "history",
                "url": "https://example.com/seen?utm_source=briefing",
                "title": "Used before",
            }
        )
        + "\n"
    )
    assert main(["--config", str(project.config), "import", str(existing)]) == 0
    capsys.readouterr()

    assert main(
        ["--config", str(project.config), "import", "--published", str(history)]
    ) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {"inserted": 0, "job": "import", "published": True, "records": 1}


def test_import_rejects_unknown_fields(project, tmp_path, capsys) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps(
            {
                "provider": "x",
                "url": "https://example.com/post",
                "title": "Post",
                "secret": "must not pass through",
            }
        )
        + "\n"
    )

    assert main(["--config", str(project.config), "import", str(path)]) == 1

    captured = capsys.readouterr()
    assert "unknown fields: secret" in captured.err
    assert captured.out == ""


def test_import_rejects_explicit_null(project, tmp_path, capsys) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(
        json.dumps(
            {
                "provider": "x",
                "url": "https://example.com/post",
                "title": "Post",
                "content": None,
            }
        )
        + "\n"
    )

    assert main(["--config", str(project.config), "import", str(path)]) == 1

    captured = capsys.readouterr()
    assert "field content must be a string" in captured.err
    assert "Traceback" not in captured.err


def test_import_normalizes_timezone_aware_timestamp() -> None:
    stream = StringIO(
        json.dumps(
            {
                "provider": "x",
                "url": "https://example.com/post",
                "title": "Post",
                "fetched_at": "2026-08-05T12:30:00+02:00",
            }
        )
        + "\n"
    )

    source = _read_sources(stream)[0]

    assert source.fetched_at == "2026-08-05T10:30:00+00:00"


def test_import_rejects_timestamp_without_timezone() -> None:
    stream = StringIO(
        json.dumps(
            {
                "provider": "x",
                "url": "https://example.com/post",
                "title": "Post",
                "fetched_at": "2026-08-05T12:30:00",
            }
        )
        + "\n"
    )

    with pytest.raises(ValueError, match="timezone-aware ISO timestamp"):
        _read_sources(stream)


def test_import_bounds_each_read(monkeypatch) -> None:
    monkeypatch.setattr(cli, "MAX_IMPORT_LINE_CHARS", 20)

    with pytest.raises(ValueError, match="line 1 is too large"):
        _read_sources(StringIO("x" * 21))


def test_import_caps_total_size(monkeypatch) -> None:
    monkeypatch.setattr(cli, "MAX_IMPORT_CHARS", 100)

    with pytest.raises(ValueError, match="exceeds 100 characters"):
        _read_sources(StringIO("\n" * 101))


def test_import_caps_record_count(monkeypatch) -> None:
    monkeypatch.setattr(cli, "MAX_IMPORT_RECORDS", 1)
    line = json.dumps(
        {"provider": "x", "url": "https://example.com/post", "title": "Post"}
    )

    with pytest.raises(ValueError, match="at most 1 source records"):
        _read_sources(StringIO(f"{line}\n{line}\n"))
