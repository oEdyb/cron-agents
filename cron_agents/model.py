from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from cron_agents.config import ModelConfig


class ModelError(RuntimeError):
    pass


def run_model(model: ModelConfig, prompt: str, *, cwd: Path) -> str:
    environment = {name: os.environ[name] for name in model.env if name in os.environ}
    command = tuple(prompt if part == "{prompt}" else part for part in model.command)
    stdin = None if "{prompt}" in model.command else prompt
    try:
        result = subprocess.run(
            command,
            input=stdin,
            text=True,
            capture_output=True,
            timeout=model.timeout,
            cwd=cwd,
            env=environment,
            check=False,
        )
    except FileNotFoundError as error:
        raise ModelError(f"model command not found: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise ModelError(f"model command timed out after {model.timeout}s") from error

    if result.returncode != 0:
        detail = result.stderr.strip() or "no error output"
        raise ModelError(f"model command exited {result.returncode}: {detail}")

    output = _output(result.stdout, model.output)
    if not output:
        raise ModelError("model command returned no output")
    return output


def _output(stdout: str, output_format: str) -> str:
    if output_format == "text":
        return stdout.strip()

    final = ""
    try:
        for line in stdout.splitlines():
            event = json.loads(line)
            if event.get("role") == "assistant" and isinstance(event.get("content"), str):
                final = event["content"].strip()
    except (json.JSONDecodeError, AttributeError) as error:
        raise ModelError("model command returned invalid JSONL") from error
    return final
