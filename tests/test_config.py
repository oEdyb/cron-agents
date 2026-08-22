from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cron_agents.config import ConfigError, load_config


def test_shipped_example_config_loads() -> None:
    root = Path(__file__).parent.parent

    config = load_config(root / "config.example.yaml")

    for name in ("codex", "codex-web"):
        command = config.models[name].command
        assert command[command.index("--model") + 1] == "gpt-5.6-sol"
        reasoning = 'model_reasoning_effort="xhigh"'
        assert command.count(reasoning) == 1
        assert command[command.index(reasoning) - 1] == "-c"
        assert command.count("--disable") == 2
        assert "apps" in command
        assert "image_generation" in command
    assert config.models["codex"].command[-1] == "-"
    assert "--strict-config" in config.models["codex"].command
    assert 'web_search="disabled"' in config.models["codex"].command
    assert config.models["codex-web"].command[-1] == "-"
    assert "--strict-config" in config.models["codex-web"].command
    assert 'web_search="live"' in config.models["codex-web"].command
    assert "features.shell_tool=false" in config.models["codex-web"].command
    assert config.models["codex"].timeout == 600
    assert config.models["codex-web"].timeout == 1800
    luna = config.models["codex-luna"]
    assert luna.command[luna.command.index("--model") + 1] == "gpt-5.6-luna"
    assert 'model_reasoning_effort="low"' in luna.command
    assert 'web_search="disabled"' in luna.command
    assert "features.shell_tool=false" in luna.command
    assert luna.timeout == 600
    assert config.models["kimi"].command[0] == "kimi"
    assert config.models["kimi"].command[-1] == "{prompt}"
    assert config.models["kimi"].output == "jsonl"
    assert config.models["kimi-web"].command[0] == "kimi"
    assert config.agents["reader"].model == "codex-luna"
    assert config.agents["curator"].model == "codex"
    assert config.agents["writer"].model == "codex-web"
    assert config.jobs["briefing"].settings["reader"] == "reader"
    assert config.jobs["briefing"].settings["min_sources"] == 1
    assert "max_sources" not in config.jobs["briefing"].settings
    assert config.jobs["briefing"].settings["candidate_limit"] == 1000
    assert config.jobs["papers"].settings == {}
    curator_prompt = (root / "prompts" / "curator.md").read_text()
    writer_prompt = (root / "prompts" / "writer.md").read_text()
    curator_text = " ".join(curator_prompt.split())
    writer_text = " ".join(writer_prompt.split())
    assert len(curator_prompt.split()) < 230
    assert len(writer_prompt.split()) < 700
    assert "content idea, project, experiment, or comparison" in curator_text
    assert "real curiosity" in curator_text
    assert "compounding knowledge" in curator_text
    assert "concrete case" in curator_text
    assert "today's limited reading time" in curator_text
    assert "There is no target count." in curator_text
    assert "a first opinion, not a command" in curator_text
    assert "technically capable generalist" in writer_text
    assert "smart friend" in writer_text
    assert "simple picture of the problem and change" in writer_text
    assert "one paragraph do one job" in writer_text
    assert "Explain a necessary technical term" in writer_text
    assert "Keep the main result" in writer_text
    assert "fast map for deciding what to study" in writer_text
    assert "120 to 180 words" in writer_text
    assert "under 260 words" in writer_text
    assert "plain technical English" in writer_text
    assert "Put a concrete case beside an abstract idea" in writer_text
    assert "**Example:**" not in writer_text
    assert "**What happened:**" in writer_text
    assert "**Why it matters:**" in writer_text
    assert "**Bigger picture:**" in writer_text
    assert "Do not pad labels or force an example" in writer_text
    assert "Leave supporting detail to the source" in writer_text
    assert "**Try it:**" in writer_text
    assert "Write only the marker, not a link or URL" in writer_text
    assert "keeps every link tied to an approved source" in writer_text
    assert "Cite every selected record as the plain text marker `[source:ID]`" in writer_text
    context = config.jobs["briefing"].settings["context"]
    assert "preserve useful variety" in context
    assert "plain technical" in context
    assert "technically capable generalist" in context
    assert "may be new to each source's field" in context


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
