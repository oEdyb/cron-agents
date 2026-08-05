from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TextIO

from cron_agents.config import ConfigError, load_config
from cron_agents.db import Database, Source
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
    import_command = subcommands.add_parser("import", help="import source records from JSONL")
    import_command.add_argument(
        "--published",
        action="store_true",
        help="mark imported records as already used",
    )
    import_command.add_argument("path", help="JSONL file, or - for stdin")
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
        name=job_name,
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


SOURCE_FIELDS = {"provider", "provider_id", "url", "title", "content", "author", "fetched_at"}
SOURCE_REQUIRED = {"provider", "url", "title"}
MAX_IMPORT_CHARS = 25_000_000
MAX_IMPORT_LINE_CHARS = 250_000
MAX_IMPORT_RECORDS = 1000


def _source(value: object, line_number: int) -> Source:
    if not isinstance(value, dict):
        raise ValueError(f"import line {line_number} must be a JSON object")
    unknown = sorted(set(value) - SOURCE_FIELDS)
    if unknown:
        raise ValueError(f"import line {line_number} has unknown fields: {', '.join(unknown)}")
    missing = sorted(SOURCE_REQUIRED - set(value))
    if missing:
        raise ValueError(f"import line {line_number} is missing: {', '.join(missing)}")
    for name in SOURCE_FIELDS & set(value):
        if not isinstance(value[name], str):
            raise ValueError(f"import line {line_number} field {name} must be a string")
    fetched_at = value.get("fetched_at")
    if fetched_at is not None:
        try:
            parsed_time = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(
                f"import line {line_number} fetched_at must be a timezone-aware ISO timestamp"
            ) from error
        if parsed_time.tzinfo is None or parsed_time.utcoffset() is None:
            raise ValueError(
                f"import line {line_number} fetched_at must be a timezone-aware ISO timestamp"
            )
        fetched_at = parsed_time.astimezone(UTC).isoformat(timespec="seconds")
    return Source.create(
        provider=value["provider"],
        provider_id=value.get("provider_id"),
        url=value["url"],
        title=value["title"],
        content=value.get("content", ""),
        author=value.get("author"),
        fetched_at=fetched_at,
    )


def _read_sources(stream: TextIO) -> list[Source]:
    sources: list[Source] = []
    total_chars = 0
    line_number = 0
    while line := stream.readline(MAX_IMPORT_LINE_CHARS + 1):
        line_number += 1
        total_chars += len(line)
        if len(line) > MAX_IMPORT_LINE_CHARS:
            raise ValueError(f"import line {line_number} is too large")
        if total_chars > MAX_IMPORT_CHARS:
            raise ValueError(f"import exceeds {MAX_IMPORT_CHARS} characters")
        if not line.strip():
            continue
        if len(sources) == MAX_IMPORT_RECORDS:
            raise ValueError(
                f"one import can contain at most {MAX_IMPORT_RECORDS} source records"
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"import line {line_number} is not valid JSON") from error
        sources.append(_source(value, line_number))
    if not sources:
        raise ValueError("import contains no source records")
    return sources


def import_sources(config_path: str | Path, path: str, published: bool) -> dict[str, object]:
    config = load_config(config_path)
    database = Database(config.state_dir / "state.db")
    database.initialize()
    input_path = Path(path).expanduser() if path != "-" else None
    opened = (
        nullcontext(sys.stdin)
        if input_path is None
        else input_path.open(encoding="utf-8")
    )
    with opened as stream:
        sources = _read_sources(stream)
    inserted = database.add_sources(sources, published=published)
    return {
        "job": "import",
        "records": len(sources),
        "inserted": inserted,
        "published": published,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = run_job(args.config, args.job, args.date)
        else:
            result = import_sources(args.config, args.path, args.published)
    except (ConfigError, ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
