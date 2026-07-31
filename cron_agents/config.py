from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    command: tuple[str, ...]
    timeout: int
    env: tuple[str, ...]
    output: str = "text"


@dataclass(frozen=True)
class AgentConfig:
    model: str
    prompt: Path


@dataclass(frozen=True)
class JobConfig:
    module: str
    settings: dict[str, Any]


@dataclass(frozen=True)
class Config:
    root: Path
    state_dir: Path
    models: dict[str, ModelConfig]
    agents: dict[str, AgentConfig]
    jobs: dict[str, JobConfig]


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _string_list(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be a non-empty list of strings")
    return tuple(value)


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as error:
        raise ConfigError(f"invalid YAML in {config_path}: {error}") from error
    root_data = _mapping(raw, "config")
    root = config_path.parent

    state_value = _string(root_data.get("state_dir", "data"), "state_dir")
    state_dir = (root / state_value).resolve()

    models: dict[str, ModelConfig] = {}
    for name, value in _mapping(root_data.get("models"), "models").items():
        model = _mapping(value, f"models.{name}")
        command = _string_list(model.get("command"), f"models.{name}.command")
        timeout = model.get("timeout", 600)
        if not isinstance(timeout, int) or timeout < 1:
            raise ConfigError(f"models.{name}.timeout must be a positive integer")
        env_value = model.get("env", ["PATH", "HOME"])
        env = _string_list(env_value, f"models.{name}.env")
        output = model.get("output", "text")
        if not isinstance(output, str) or output not in {"text", "jsonl"}:
            raise ConfigError(f"models.{name}.output must be text or jsonl")
        models[name] = ModelConfig(command=command, timeout=timeout, env=env, output=output)

    agents: dict[str, AgentConfig] = {}
    for name, value in _mapping(root_data.get("agents"), "agents").items():
        agent = _mapping(value, f"agents.{name}")
        model_name = _string(agent.get("model"), f"agents.{name}.model")
        if model_name not in models:
            raise ConfigError(f"agents.{name}.model references unknown model: {model_name}")
        prompt_value = _string(agent.get("prompt"), f"agents.{name}.prompt")
        prompt = (root / prompt_value).resolve()
        if not prompt.is_file():
            raise ConfigError(f"agents.{name}.prompt not found: {prompt}")
        agents[name] = AgentConfig(model=model_name, prompt=prompt)

    jobs: dict[str, JobConfig] = {}
    for name, value in _mapping(root_data.get("jobs"), "jobs").items():
        job = _mapping(value, f"jobs.{name}").copy()
        module = _string(job.pop("module", None), f"jobs.{name}.module")
        jobs[name] = JobConfig(module=module, settings=job)

    if not models:
        raise ConfigError("models must not be empty")
    if not agents:
        raise ConfigError("agents must not be empty")
    if not jobs:
        raise ConfigError("jobs must not be empty")

    return Config(root=root, state_dir=state_dir, models=models, agents=agents, jobs=jobs)
