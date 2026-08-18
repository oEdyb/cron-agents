from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from cron_agents.jobs import briefing

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setattr(briefing, "_now", lambda: datetime(2026, 7, 31, 20, tzinfo=UTC))
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "curator.md").write_text("Choose sources.")
    (tmp_path / "prompts" / "reader.md").write_text("Read source material.")
    (tmp_path / "prompts" / "writer.md").write_text("Write the briefing.")
    log = tmp_path / "model.log"
    monkeypatch.setenv("MODEL_LOG", str(log))

    config = {
        "state_dir": "data",
        "models": {
            "fixture": {
                "command": [sys.executable, str(FIXTURES / "model.py")],
                "timeout": 10,
                "env": [
                    "PATH",
                    "HOME",
                    "MODEL_LOG",
                    "MODEL_FAIL_WRITER",
                    "MODEL_FAIL_EDITOR",
                    "MODEL_READER_DROP_CARD",
                    "MODEL_READER_UNKNOWN_CARD",
                    "MODEL_EDITOR_UNKNOWN_CITATION",
                    "MODEL_EDITOR_DROP_CITATION",
                    "MODEL_EDITOR_NO_COMPLETE",
                    "MODEL_INVALID_CURATOR",
                    "MODEL_INVALID_CURATOR_ONCE",
                    "MODEL_CHECKER_SWAP",
                    "MODEL_CHECKER_SWAP_ONCE",
                    "MODEL_SELECT_ALL",
                    "MODEL_UNKNOWN_CITATION",
                ],
            }
        },
        "agents": {
            "reader": {"model": "fixture", "prompt": "prompts/reader.md"},
            "curator": {"model": "fixture", "prompt": "prompts/curator.md"},
            "writer": {"model": "fixture", "prompt": "prompts/writer.md"},
            "editor": {"model": "fixture", "prompt": "prompts/writer.md"},
        },
        "jobs": {
            "rss": {
                "module": "cron_agents.jobs.rss",
                "limit_per_feed": 20,
                "feeds": [{"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()}],
            },
            "briefing": {
                "module": "cron_agents.jobs.briefing",
                "curator": "curator",
                "reader": "reader",
                "writer": "writer",
                "editor": "editor",
                "context": "Fixture briefing.",
                "lookback_hours": 36,
                "min_sources": 2,
                "max_sources": 2,
                "max_content_chars": 1000,
                "output_dir": "briefings",
            },
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return SimpleNamespace(root=tmp_path, config=path, log=log, data=config)


def read_log(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return [json_line(line) for line in path.read_text().splitlines()]


def json_line(line: str) -> dict[str, str]:
    import json

    return json.loads(line)
