from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from cron_agents.config import ConfigError, load_config
from cron_agents.db import Database
from cron_agents.jobs import JobContext


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="cron-agents")
    command.add_argument("--config", default="config.yaml", help="path to config.yaml")
    subcommands = command.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="run one job")
    run.add_argument("job", help="job name from config.yaml")
    run.add_argument(
        "--date",
        type=_date,
        default=datetime.now(UTC).date(),
        help="briefing date in UTC",
    )
    return command


def run_job(config_path: str | Path, job_name: str, run_date: date) -> dict[str, object]:
    config = load_config(config_path)
    if job_name not in config.jobs:
        available = ", ".join(sorted(config.jobs))
        raise ConfigError(f"unknown job {job_name!r}; choose one of: {available}")

    database = Database(config.state_dir / "state.db")
    database.initialize()
    job = config.jobs[job_name]
    module = importlib.import_module(job.module)
    run = getattr(module, "run", None)
    if not callable(run):
        raise ConfigError(f"{job.module} must define run(ctx)")

    context = JobContext(
        root=config.root,
        config=config,
        job=job,
        database=database,
        date=run_date,
    )
    result = run(context)
    if not isinstance(result, dict):
        raise TypeError(f"{job.module}.run(ctx) must return a dictionary")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = run_job(args.config, args.job, args.date)
    except (ConfigError, ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
