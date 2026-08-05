from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from cron_agents.config import Config, JobConfig
from cron_agents.db import Database


@dataclass(frozen=True)
class JobContext:
    name: str
    root: Path
    config: Config
    job: JobConfig
    database: Database
    date: date


def fetch_content(url: str, *, timeout: int = 30, max_bytes: int = 5_000_000) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "cron-agents/0.1"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured source URL
        content = response.read(max_bytes + 1)
        final_url = getattr(response, "geturl", lambda: url)()
    if len(content) > max_bytes:
        raise ValueError(f"response exceeds {max_bytes} bytes: {url}")
    return content, final_url


def fetch_bytes(url: str, *, timeout: int = 30, max_bytes: int = 5_000_000) -> bytes:
    return fetch_content(url, timeout=timeout, max_bytes=max_bytes)[0]


def fetch_json(url: str, *, timeout: int = 30) -> Any:
    return json.loads(fetch_bytes(url, timeout=timeout))
