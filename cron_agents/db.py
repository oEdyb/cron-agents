from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_KEYS = {"fbclid", "gclid", "ref", "source"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"source URL must use http or https: {value}")

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    default_port = (parsed.scheme, port) in {("http", 80), ("https", 443)}
    if port and not default_port:
        hostname = f"{hostname}:{port}"

    path = parsed.path or "/"
    drop_query = False
    query_value = parsed.query
    paper = re.fullmatch(r"/papers/([^/]+)/?", path)
    if hostname in {"huggingface.co", "www.huggingface.co"} and paper:
        scheme = "https"
        hostname = "arxiv.org"
        path = f"/abs/{paper.group(1)}"
        drop_query = True
    status = re.fullmatch(r"/(?:i/web|[^/]+)/status/(\d+)(?:/.*)?", path)
    x_hosts = {"twitter.com", "www.twitter.com", "mobile.twitter.com", "x.com", "www.x.com"}
    if hostname in x_hosts and status:
        scheme = "https"
        hostname = "x.com"
        path = f"/i/status/{status.group(1)}"
        drop_query = True
    if hostname in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"} and path.startswith(
        "/abs/"
    ):
        scheme = "https"
        hostname = "arxiv.org"
        path = re.sub(r"v\d+$", "", path)
        drop_query = True
    youtube_id = None
    if hostname == "youtu.be":
        youtube_id = path.strip("/").split("/", 1)[0]
    elif hostname in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }:
        if path.rstrip("/") == "/watch":
            youtube_id = dict(parse_qsl(parsed.query)).get("v")
        else:
            video_path = re.fullmatch(r"/(?:embed|live|shorts)/([^/]+)/?", path)
            youtube_id = video_path.group(1) if video_path else None
    if youtube_id and re.fullmatch(r"[A-Za-z0-9_-]{6,20}", youtube_id):
        scheme = "https"
        hostname = "www.youtube.com"
        path = "/watch"
        query_value = urlencode({"v": youtube_id})
    if path != "/":
        path = path.rstrip("/")

    query = [
        (key, item)
        for key, item in parse_qsl("" if drop_query else query_value, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_KEYS
    ]
    return urlunsplit((scheme, hostname, path, urlencode(sorted(query)), ""))


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


@dataclass(frozen=True)
class Source:
    id: str
    provider: str
    provider_id: str | None
    url: str
    title: str
    content: str
    author: str | None
    fetched_at: str
    source_published_at: str | None
    fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        provider_id: str | None,
        url: str,
        title: str,
        content: str = "",
        author: str | None = None,
        fetched_at: str | None = None,
        source_published_at: str | None = None,
    ) -> Source:
        provider = provider.strip()
        title = title.strip()
        content = content.strip()
        if not provider or not title:
            raise ValueError("source provider and title are required")

        clean_url = canonical_url(url)
        clean_provider_id = provider_id.strip() if provider_id else None
        source_prefix = re.sub(r"[^a-z0-9._-]+", "-", provider.casefold()).strip("-")
        if clean_provider_id:
            if re.fullmatch(r"[A-Za-z0-9._-]{1,100}", clean_provider_id):
                stable_id = clean_provider_id
            else:
                stable_id = hashlib.sha256(clean_provider_id.encode()).hexdigest()[:24]
            source_id = f"{source_prefix}:{stable_id}"
        else:
            digest = hashlib.sha256(clean_url.encode()).hexdigest()[:24]
            source_id = f"url:{digest}"

        body = f"{_normalized_text(title)}\n{_normalized_text(content)}"
        if len(_normalized_text(content)) < 80:
            body = f"{body}\n{clean_url}"
        fingerprint = hashlib.sha256(body.encode()).hexdigest()

        return cls(
            id=source_id,
            provider=provider,
            provider_id=clean_provider_id,
            url=clean_url,
            title=title,
            content=content,
            author=author.strip() if author else None,
            fetched_at=fetched_at or utc_now(),
            source_published_at=source_published_at,
            fingerprint=fingerprint,
        )

    def prompt_record(self, max_content_chars: int) -> dict[str, str | None]:
        return {
            "id": self.id,
            "provider": self.provider,
            "url": self.url,
            "title": self.title,
            "content": self.content[:max_content_chars],
            "author": self.author,
            "fetched_at": self.fetched_at,
            "published_at": self.source_published_at,
        }


class Database:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_id TEXT,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    author TEXT,
                    fetched_at TEXT NOT NULL,
                    source_published_at TEXT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'fetched'
                        CHECK (status IN ('fetched', 'published')),
                    published_at TEXT
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
            if "source_published_at" not in columns:
                connection.execute("ALTER TABLE sources ADD COLUMN source_published_at TEXT")

    def add_sources(self, sources: list[Source], *, published: bool = False) -> int:
        inserted = 0
        published_at = utc_now() if published else None
        with self._connect() as connection:
            for source in sources:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO sources (
                        id, provider, provider_id, url, title, content, author,
                        fetched_at, source_published_at, fingerprint, status, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source.id,
                        source.provider,
                        source.provider_id,
                        source.url,
                        source.title,
                        source.content,
                        source.author,
                        source.fetched_at,
                        source.source_published_at,
                        source.fingerprint,
                        "published" if published else "fetched",
                        published_at,
                    ),
                )
                inserted += cursor.rowcount
                if published and cursor.rowcount == 0:
                    rows = connection.execute(
                        """
                        SELECT id FROM sources
                        WHERE id = ? OR url = ? OR fingerprint = ?
                        """,
                        (source.id, source.url, source.fingerprint),
                    ).fetchall()
                    if not rows:
                        raise RuntimeError(f"could not find duplicate source: {source.id}")
                    matched_ids = {row["id"] for row in rows}
                    if len(matched_ids) != 1:
                        raise ValueError(
                            f"published import {source.id} matches multiple existing sources"
                        )
                    connection.execute(
                        """
                        UPDATE sources
                        SET status = 'published', published_at = COALESCE(published_at, ?)
                        WHERE id = ?
                        """,
                        (published_at, matched_ids.pop()),
                    )
        return inserted

    def available_sources(
        self,
        *,
        since: str,
        before: str,
        excluded_ids: set[str],
        limit: int,
    ) -> list[Source]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, provider, provider_id, url, title, content, author,
                       fetched_at, source_published_at, fingerprint
                FROM sources
                WHERE status = 'fetched'
                  AND fetched_at < ?
                  AND COALESCE(source_published_at, fetched_at) >= ?
                  AND COALESCE(source_published_at, fetched_at) < ?
                ORDER BY COALESCE(source_published_at, fetched_at) DESC, fetched_at DESC, id
                """,
                (before, since, before),
            ).fetchall()
        buckets: dict[str, deque[Source]] = {}
        for row in rows:
            if row["id"] in excluded_ids:
                continue
            source = self._from_row(row)
            buckets.setdefault(source.provider, deque()).append(source)

        available: list[Source] = []
        while len(available) < limit:
            added = False
            for bucket in buckets.values():
                if bucket:
                    available.append(bucket.popleft())
                    added = True
                    if len(available) == limit:
                        break
            if not added:
                break
        return available

    def get_sources(self, source_ids: list[str]) -> list[Source]:
        if not source_ids:
            return []
        placeholders = ",".join("?" for _ in source_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, provider, provider_id, url, title, content, author,
                       fetched_at, source_published_at, fingerprint
                FROM sources
                WHERE id IN ({placeholders})
                """,  # noqa: S608 - placeholders are generated, not user input
                source_ids,
            ).fetchall()
        by_id = {row["id"]: self._from_row(row) for row in rows}
        return [by_id[source_id] for source_id in source_ids if source_id in by_id]

    def mark_published(self, source_ids: list[str]) -> None:
        if not source_ids:
            raise ValueError("cannot publish an empty source selection")
        published_at = utc_now()
        with self._connect() as connection:
            for source_id in source_ids:
                cursor = connection.execute(
                    """
                    UPDATE sources
                    SET status = 'published', published_at = COALESCE(published_at, ?)
                    WHERE id = ?
                    """,
                    (published_at, source_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"unknown source ID: {source_id}")

    def status(self, source_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return row["status"] if row else None

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"],
            provider=row["provider"],
            provider_id=row["provider_id"],
            url=row["url"],
            title=row["title"],
            content=row["content"],
            author=row["author"],
            fetched_at=row["fetched_at"],
            source_published_at=row["source_published_at"],
            fingerprint=row["fingerprint"],
        )
