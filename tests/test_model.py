from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cron_agents.config import ModelConfig
from cron_agents.model import ModelError, run_model


def test_sends_prompt_and_only_allowlisted_environment(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "model.py"
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'prompt': sys.stdin.read(), 'safe': os.getenv('SAFE'), "
        "'secret': os.getenv('SECRET'), 'cwd': os.getcwd()}))\n"
    )
    monkeypatch.setenv("SAFE", "yes")
    monkeypatch.setenv("SECRET", "no")
    model = ModelConfig(
        command=(sys.executable, str(script)),
        timeout=5,
        env=("PATH", "SAFE"),
    )

    output = run_model(model, "hello", cwd=tmp_path)

    import json

    result = json.loads(output)
    assert result == {"prompt": "hello", "safe": "yes", "secret": None, "cwd": str(tmp_path)}


def test_can_send_prompt_as_argument(tmp_path: Path) -> None:
    model = ModelConfig(
        command=(sys.executable, "-c", "import sys; print(sys.argv[1])", "{prompt}"),
        timeout=5,
        env=("PATH",),
    )

    assert run_model(model, "hello", cwd=tmp_path) == "hello"


def test_extracts_last_assistant_message_from_jsonl(tmp_path: Path) -> None:
    script = (
        "import json; "
        "print(json.dumps({'role': 'meta', 'type': 'start'})); "
        "print(json.dumps({'role': 'assistant', 'content': 'first'})); "
        "print(json.dumps({'role': 'assistant', 'content': 'final'}))"
    )
    model = ModelConfig(
        command=(sys.executable, "-c", script),
        timeout=5,
        env=("PATH",),
        output="jsonl",
    )

    assert run_model(model, "hello", cwd=tmp_path) == "final"


def test_rejects_invalid_jsonl(tmp_path: Path) -> None:
    model = ModelConfig(
        command=(sys.executable, "-c", "print('not json')"),
        timeout=5,
        env=("PATH",),
        output="jsonl",
    )

    with pytest.raises(ModelError, match="invalid JSONL"):
        run_model(model, "hello", cwd=tmp_path)


def test_reports_nonzero_exit(tmp_path: Path) -> None:
    model = ModelConfig(
        command=(sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"),
        timeout=5,
        env=("PATH",),
    )

    with pytest.raises(ModelError, match="exited 7: bad"):
        run_model(model, "prompt", cwd=tmp_path)


def test_reports_timeout(tmp_path: Path) -> None:
    model = ModelConfig(
        command=(sys.executable, "-c", "import time; time.sleep(2)"),
        timeout=1,
        env=("PATH",),
    )

    with pytest.raises(ModelError, match="timed out after 1s"):
        run_model(model, "prompt", cwd=tmp_path)


def test_reports_empty_output(tmp_path: Path) -> None:
    model = ModelConfig(
        command=(sys.executable, "-c", "pass"),
        timeout=5,
        env=("PATH",),
    )

    with pytest.raises(ModelError, match="returned no output"):
        run_model(model, "prompt", cwd=tmp_path)
