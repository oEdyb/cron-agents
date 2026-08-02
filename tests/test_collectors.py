from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest

from cron_agents import jobs
from cron_agents.config import Config, JobConfig
from cron_agents.db import Database, Source
from cron_agents.jobs import JobContext, hn, rss

FIXTURES = Path(__file__).parent / "fixtures"


def context(tmp_path: Path, settings: dict[str, object]) -> JobContext:
    database = Database(tmp_path / "state.db")
    database.initialize()
    job = JobConfig(module="test", settings=settings)
    config = Config(root=tmp_path, state_dir=tmp_path, models={}, agents={}, jobs={})
    return JobContext(tmp_path, config, job, database, date(2026, 7, 31))


def test_rss_collects_local_fixture(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()}]},
    )

    result = rss.run(ctx)

    assert result == {"job": "rss", "fetched": 3, "inserted": 3}


def test_rss_collects_atom_fixture(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "fixture", "url": (FIXTURES / "feed.atom").as_uri()}]},
    )

    result = rss.run(ctx)
    item = ctx.database.available_sources(
        since="",
        before="9999-12-31T23:59:59+00:00",
        excluded_ids=set(),
        limit=1,
    )[0]

    assert result == {"job": "rss", "fetched": 1, "inserted": 1}
    assert item.provider_id == "urn:uuid:1225c695-cfb8-4ebb-aaaa-80da344efa6a"
    assert item.id.startswith("rss-fixture:")
    assert item.url == "https://example.com/atom-entry"
    assert item.title == "Atom entry"
    assert item.content == "Useful Atom details."
    assert item.author == "Ada"


def test_rss_uses_permalink_guid_when_link_is_missing(tmp_path: Path) -> None:
    feed = tmp_path / "feed.xml"
    feed.write_text(
        "<rss version='2.0'><channel><title>x</title><link>https://example.com</link>"
        "<description>x</description><item><title>Entry</title>"
        "<guid>https://example.com/guid-entry</guid></item></channel></rss>"
    )
    ctx = context(tmp_path, {"feeds": [{"name": "fixture", "url": feed.as_uri()}]})

    result = rss.run(ctx)

    assert result == {"job": "rss", "fetched": 1, "inserted": 1}


def test_fetch_rejects_oversized_response(monkeypatch) -> None:
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    monkeypatch.setattr(jobs, "urlopen", lambda *_args, **_kwargs: Response(b"1234"))

    with pytest.raises(ValueError, match="response exceeds 3 bytes"):
        jobs.fetch_bytes("https://example.com/feed", max_bytes=3)


def test_fetch_returns_final_url_after_redirect(monkeypatch) -> None:
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def geturl(self):
            return "https://example.com/final/feed.xml"

    monkeypatch.setattr(jobs, "urlopen", lambda *_args, **_kwargs: Response(b"feed"))

    assert jobs.fetch_content("https://example.com/start") == (
        b"feed",
        "https://example.com/final/feed.xml",
    )


def test_atom_resolves_xml_base_from_redirected_document(tmp_path: Path, monkeypatch) -> None:
    document = b"""\
    <feed xmlns="http://www.w3.org/2005/Atom" xml:base="news/">
      <entry xml:base="../items/">
        <title>Relative entry</title>
        <id>relative-1</id>
        <link href="one" />
      </entry>
    </feed>
    """
    monkeypatch.setattr(
        rss,
        "fetch_content",
        lambda _url: (document, "https://example.com/redirected/feed.xml"),
    )
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "fixture", "url": "https://example.com/start"}]},
    )

    rss.run(ctx)
    item = ctx.database.get_sources(["rss-fixture:relative-1"])[0]

    assert item.url == "https://example.com/redirected/items/one"


def test_rss_rejects_malformed_xml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        rss,
        "fetch_content",
        lambda _url: (b"<rss>", "https://example.com/feed.xml"),
    )
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "fixture", "url": "https://example.com/feed.xml"}]},
    )

    with pytest.raises(ValueError, match="invalid RSS or Atom XML"):
        rss.run(ctx)


def test_hn_collects_stories(tmp_path: Path, monkeypatch) -> None:
    payloads = {
        "https://hn.test/topstories.json": [1, 2],
        "https://hn.test/item/1.json": {
            "id": 1,
            "type": "story",
            "title": "One",
            "url": "https://example.com/one",
            "by": "ada",
        },
        "https://hn.test/item/2.json": {"id": 2, "type": "comment", "text": "skip"},
    }
    monkeypatch.setattr(hn, "fetch_json", payloads.__getitem__)
    ctx = context(tmp_path, {"base_url": "https://hn.test", "limit": 2})

    result = hn.run(ctx)
    item = ctx.database.get_sources(["hn:1"])[0]

    assert result == {"job": "hn", "fetched": 1, "inserted": 1, "reader_failures": 0}
    assert item.title == "One"
    assert item.url == "https://example.com/one"
    assert item.author == "ada"


def test_hn_reader_enriches_new_linked_story(tmp_path: Path, monkeypatch) -> None:
    payloads = {
        "https://hn.test/topstories.json": [1],
        "https://hn.test/item/1.json": {
            "id": 1,
            "type": "story",
            "title": "One",
            "url": "https://example.com/one",
        },
    }
    monkeypatch.setattr(hn, "fetch_json", payloads.__getitem__)
    requested: list[str] = []

    def reader(url: str, **_kwargs):
        requested.append(url)
        return b"Concrete article facts.", url

    monkeypatch.setattr(hn, "fetch_content", reader)
    ctx = context(
        tmp_path,
        {"base_url": "https://hn.test", "limit": 1, "reader_url": "https://reader.test/"},
    )

    result = hn.run(ctx)
    item = ctx.database.get_sources(["hn:1"])[0]

    assert result == {"job": "hn", "fetched": 1, "inserted": 1, "reader_failures": 0}
    assert requested == ["https://reader.test/https://example.com/one"]
    assert item.content == "Concrete article facts."


def test_hn_reader_skips_source_already_in_ledger(tmp_path: Path, monkeypatch) -> None:
    ctx = context(
        tmp_path,
        {"base_url": "https://hn.test", "limit": 1, "reader_url": "https://reader.test/"},
    )
    ctx.database.add_sources(
        [
            Source.create(
                provider="hn",
                provider_id="1",
                url="https://example.com/one",
                title="One",
            )
        ]
    )
    monkeypatch.setattr(
        hn, "fetch_json", lambda url: [1] if url.endswith("topstories.json") else None
    )
    monkeypatch.setattr(
        hn,
        "fetch_content",
        lambda *_args, **_kwargs: pytest.fail("reader should not fetch a known source"),
    )

    result = hn.run(ctx)

    assert result == {"job": "hn", "fetched": 0, "inserted": 0, "reader_failures": 0}


def test_hn_reader_failure_does_not_store_title_only_source(tmp_path: Path, monkeypatch) -> None:
    payloads = {
        "https://hn.test/topstories.json": [1],
        "https://hn.test/item/1.json": {
            "id": 1,
            "type": "story",
            "title": "One",
            "url": "https://example.com/one",
        },
    }
    monkeypatch.setattr(hn, "fetch_json", payloads.__getitem__)
    monkeypatch.setattr(
        hn,
        "fetch_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("reader unavailable")),
    )
    ctx = context(
        tmp_path,
        {"base_url": "https://hn.test", "limit": 1, "reader_url": "https://reader.test/"},
    )

    result = hn.run(ctx)

    assert result == {"job": "hn", "fetched": 0, "inserted": 0, "reader_failures": 1}
    assert ctx.database.status("hn:1") is None


def test_hn_rejects_limit_above_documented_maximum(tmp_path: Path) -> None:
    ctx = context(tmp_path, {"limit": 501})

    with pytest.raises(ValueError, match="between 1 and 500"):
        hn.run(ctx)
