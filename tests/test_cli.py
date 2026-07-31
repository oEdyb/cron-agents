from __future__ import annotations

import json

from cron_agents.cli import main
from cron_agents.jobs import rss


def test_full_local_fixture_run(project, capsys, monkeypatch) -> None:
    monkeypatch.setattr(rss, "utc_now", lambda: "2026-07-31T12:00:00+00:00")
    assert main(["--config", str(project.config), "run", "rss"]) == 0
    rss_result = json.loads(capsys.readouterr().out)
    assert rss_result == {"fetched": 3, "inserted": 3, "job": "rss"}

    assert main(["--config", str(project.config), "run", "briefing", "--date", "2026-07-31"]) == 0
    briefing_result = json.loads(capsys.readouterr().out)
    assert briefing_result["sources"] == 2
    assert (project.root / "briefings" / "2026-07-31.md").is_file()


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
