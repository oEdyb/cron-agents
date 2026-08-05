from __future__ import annotations

from pathlib import Path

import pytest

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


def test_canonical_url_merges_arxiv_versions_and_schemes() -> None:
    assert canonical_url("http://www.arxiv.org/abs/2607.26497v3") == (
        "https://arxiv.org/abs/2607.26497"
    )
    assert canonical_url("https://huggingface.co/papers/2607.26497v2") == (
        "https://arxiv.org/abs/2607.26497"
    )


def test_canonical_url_merges_x_and_twitter_status_links() -> None:
    expected = "https://x.com/i/status/123"

    assert canonical_url("https://twitter.com/user/status/123?s=20") == expected
    assert canonical_url("https://x.com/i/web/status/123/photo/1") == expected


def test_arxiv_and_hugging_face_copies_share_one_ledger_row(tmp_path: Path) -> None:
    db = database(tmp_path)
    hugging_face = source(
        "2607.26497",
        provider="hugging-face-papers",
        url="https://arxiv.org/abs/2607.26497",
    )
    arxiv = source(
        "2607.26497v3",
        provider="arxiv",
        url="http://www.arxiv.org/abs/2607.26497v3",
    )

    assert db.add_sources([hugging_face, arxiv]) == 1


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


def test_available_sources_rotates_across_providers(tmp_path: Path) -> None:
    db = database(tmp_path)
    sources = [
        source(
            f"a-{number}",
            provider="a",
        )
        for number in range(4)
    ]
    sources += [source(f"b-{number}", provider="b") for number in range(2)]
    db.add_sources(sources)

    available = db.available_sources(
        since="2026-07-31T00:00:00+00:00",
        before="2026-08-01T00:00:00+00:00",
        excluded_ids=set(),
        limit=4,
    )

    assert [item.provider for item in available] == ["a", "b", "a", "b"]


def test_published_import_marks_an_existing_url_seen(tmp_path: Path) -> None:
    db = database(tmp_path)
    current = source("current", provider="rss", url="https://example.com/seen")
    history = source("old", provider="history", url="https://example.com/seen?utm_source=old")
    db.add_sources([current])

    inserted = db.add_sources([history], published=True)

    assert inserted == 0
    assert db.status(current.id) == "published"


def test_published_import_rejects_matches_across_different_rows(tmp_path: Path) -> None:
    db = database(tmp_path)
    by_id = source("shared", url="https://example.com/by-id")
    by_url = source("other", url="https://example.com/by-url")
    db.add_sources([by_id, by_url])
    ambiguous = source("shared", provider="test", url="https://example.com/by-url")

    with pytest.raises(ValueError, match="matches multiple existing sources"):
        db.add_sources([ambiguous], published=True)

    assert db.status(by_id.id) == "fetched"
    assert db.status(by_url.id) == "fetched"
