from __future__ import annotations

from pathlib import Path

from cron_agents.db import Database, Source, canonical_url


def source(
    provider_id: str,
    *,
    provider: str = "test",
    url: str | None = None,
    title: str | None = None,
    content: str | None = None,
) -> Source:
    return Source.create(
        provider=provider,
        provider_id=provider_id,
        url=url or f"https://example.com/{provider_id}",
        title=title or f"Title {provider_id}",
        content=content or (f"Detailed source content for {provider_id}. " * 5),
        fetched_at="2026-07-31T12:00:00+00:00",
    )


def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "state.db")
    db.initialize()
    return db


def test_canonical_url_removes_tracking_and_fragment() -> None:
    value = "HTTPS://Example.COM:443/post/?b=2&utm_source=x&a=1#section"

    assert canonical_url(value) == "https://example.com/post?a=1&b=2"


def test_source_ids_are_safe_for_citations() -> None:
    item = source("https://example.com/posts/one?id=2")

    assert item.id.startswith("test:")
    assert "]" not in item.id
    assert len(item.id) == len("test:") + 24


def test_deduplicates_provider_id_url_and_content(tmp_path: Path) -> None:
    db = database(tmp_path)
    first = source("one")
    same_id = source("one", url="https://other.example/same-id", title="Other")
    same_url = source(
        "two",
        provider="other",
        url="https://example.com/one?utm_campaign=test",
        title="Different",
    )
    same_content = source(
        "three",
        provider="third",
        url="https://third.example/item",
        title=first.title,
        content=first.content,
    )

    assert db.add_sources([first, same_id, same_url, same_content]) == 1


def test_available_sources_excludes_reserved_and_published(tmp_path: Path) -> None:
    db = database(tmp_path)
    one, two, three = source("one"), source("two"), source("three")
    db.add_sources([one, two, three])
    db.mark_published([one.id])

    available = db.available_sources(
        since="2026-07-31T00:00:00+00:00",
        before="2026-08-01T00:00:00+00:00",
        excluded_ids={two.id},
        limit=10,
    )

    assert [item.id for item in available] == [three.id]
    assert db.status(one.id) == "published"
    assert db.status(two.id) == "fetched"
