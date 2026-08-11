from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cron_agents.config import ConfigError, load_config


def test_shipped_example_config_loads() -> None:
    root = Path(__file__).parent.parent

    config = load_config(root / "config.example.yaml")

    assert config.models["codex"].command[-1] == "-"
    assert "--strict-config" in config.models["codex"].command
    assert 'web_search="disabled"' in config.models["codex"].command
    assert config.models["codex-web"].command[-1] == "-"
    assert "--strict-config" in config.models["codex-web"].command
    assert 'web_search="live"' in config.models["codex-web"].command
    assert "features.shell_tool=false" in config.models["codex-web"].command
    assert config.models["kimi"].command[0] == "kimi"
    assert config.models["kimi"].command[-1] == "{prompt}"
    assert config.models["kimi"].output == "jsonl"
    assert config.models["kimi-web"].command[0] == "kimi"
    assert config.agents["curator"].model == "codex"
    assert config.agents["writer"].model == "codex-web"
    assert config.jobs["briefing"].settings["min_sources"] == 1
    assert config.jobs["briefing"].settings["max_sources"] == 10
    assert config.jobs["briefing"].settings["candidate_limit"] == 1000
    assert config.jobs["papers"].settings == {}
    curator_prompt = (root / "prompts" / "curator.md").read_text()
    writer_prompt = (root / "prompts" / "writer.md").read_text()
    assert "A busy feed is not an important feed." in curator_prompt
    assert "short daily lesson" in curator_prompt
    assert "learn by doing" in curator_prompt
    assert "few ideas that make the rest click" in writer_prompt
    assert "technical friend" in writer_prompt
    assert "## Try it" in writer_prompt
    assert "what to measure" in writer_prompt


def test_kimi_agent_disables_tools_and_subagents() -> None:
    root = Path(__file__).parent.parent
    parts = (root / "prompts" / "kimi.md").read_text().split("---", 2)
    frontmatter = yaml.safe_load(parts[1])

    assert frontmatter["tools"] == []
    assert frontmatter["subagents"] == []


def test_kimi_writer_has_only_search_and_fetch_tools() -> None:
    root = Path(__file__).parent.parent
    parts = (root / "prompts" / "kimi-web.md").read_text().split("---", 2)
    frontmatter = yaml.safe_load(parts[1])

    assert frontmatter["tools"] == ["WebSearch", "FetchURL"]
    assert frontmatter["subagents"] == []


def test_loads_paths_and_commands(project) -> None:
    config = load_config(project.config)

    assert config.root == project.root
    assert config.state_dir == project.root / "data"
    assert config.models["fixture"].command[0]
    assert config.agents["curator"].prompt == project.root / "prompts" / "curator.md"
    assert config.jobs["briefing"].module == "cron_agents.jobs.briefing"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data["models"]["fixture"].update(command="python"), "command"),
        (lambda data: data["models"]["fixture"].update(output="xml"), "output"),
        (lambda data: data["agents"]["writer"].update(model="missing"), "unknown model"),
        (lambda data: data.update(jobs=[]), "jobs must be a mapping"),
    ],
)
def test_rejects_invalid_config(project, change, message: str) -> None:
    change(project.data)
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))

    with pytest.raises(ConfigError, match=message):
        load_config(project.config)


def test_rejects_missing_prompt(project) -> None:
    Path(project.root / "prompts" / "writer.md").unlink()

    with pytest.raises(ConfigError, match="prompt not found"):
        load_config(project.config)


def test_rejects_malformed_yaml(project) -> None:
    project.config.write_text("models: [")

    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(project.config)
