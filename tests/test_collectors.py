from __future__ import annotations

from datetime import date
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path

import pytest

from cron_agents import jobs
from cron_agents.config import Config, JobConfig
from cron_agents.db import Database, Source
from cron_agents.jobs import JobContext, hn, papers, rss

FIXTURES = Path(__file__).parent / "fixtures"


def context(tmp_path: Path, settings: dict[str, object]) -> JobContext:
    database = Database(tmp_path / "state.db")
    database.initialize()
    job = JobConfig(module="test", settings=settings)
    config = Config(root=tmp_path, state_dir=tmp_path, models={}, agents={}, jobs={})
    return JobContext("rss", tmp_path, config, job, database, date(2026, 7, 31))


def test_rss_collects_local_fixture(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()}]},
    )

    result = rss.run(ctx)

    assert result == {"job": "rss", "fetched": 3, "inserted": 3}


def test_rss_uses_configured_job_name(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()}]},
    )
    object.__setattr__(ctx, "name", "arxiv")

    result = rss.run(ctx)

    assert result["job"] == "arxiv"


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
    assert item.source_published_at == "2026-07-31T08:00:00+00:00"


def test_rss_collects_youtube_atom_metadata(tmp_path: Path, monkeypatch) -> None:
    document = b"""\
    <feed xmlns="http://www.w3.org/2005/Atom"
          xmlns:yt="http://www.youtube.com/xml/schemas/2015"
          xmlns:media="http://search.yahoo.com/mrss/">
      <entry>
        <id>yt:video:abc123</id>
        <yt:videoId>abc123</yt:videoId>
        <title>A useful video</title>
        <link rel="alternate" href="https://www.youtube.com/watch?v=abc123" />
        <author><name>Ada Videos</name><uri>https://youtube.test/ada</uri></author>
        <published>2026-08-05T10:15:30Z</published>
        <media:group><media:description>Concrete video details.</media:description></media:group>
      </entry>
    </feed>
    """
    monkeypatch.setattr(
        rss,
        "fetch_content",
        lambda _url: (document, "https://www.youtube.com/feeds/videos.xml"),
    )
    monkeypatch.setattr(rss, "utc_now", lambda: "2026-08-05T12:00:00+00:00")
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "youtube", "url": "https://youtube.test/feed"}]},
    )

    result = rss.run(ctx)
    item = ctx.database.get_sources(["rss-youtube:abc123"])[0]

    assert result == {"job": "rss", "fetched": 1, "inserted": 1}
    assert item.content == "Concrete video details."
    assert item.author == "Ada Videos"
    assert item.fetched_at == "2026-08-05T12:00:00+00:00"
    assert item.source_published_at == "2026-08-05T10:15:30+00:00"


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

    with pytest.raises(RuntimeError, match="fixture: invalid RSS or Atom XML"):
        rss.run(ctx)


def test_rss_saves_healthy_feeds_when_another_feed_fails(tmp_path: Path) -> None:
    broken = tmp_path / "broken.xml"
    broken.write_text("<rss>")
    ctx = context(
        tmp_path,
        {
            "feeds": [
                {"name": "broken", "url": broken.as_uri()},
                {"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()},
            ]
        },
    )

    with pytest.raises(RuntimeError, match="broken"):
        rss.run(ctx)

    saved = ctx.database.available_sources(
        since="",
        before="9999-12-31T23:59:59+00:00",
        excluded_ids=set(),
        limit=10,
    )
    assert {item.title for item in saved} == {
        "First useful release",
        "Second useful release",
        "Rejected candidate",
    }


def test_rss_rejects_excessively_nested_xml(tmp_path: Path, monkeypatch) -> None:
    depth = 1500
    document = b"<rss>" + (b"<group>" * depth) + (b"</group>" * depth) + b"</rss>"
    monkeypatch.setattr(
        rss,
        "fetch_content",
        lambda _url: (document, "https://example.com/feed.xml"),
    )
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "deep", "url": "https://example.com/feed.xml"}]},
    )

    with pytest.raises(RuntimeError, match="deep: invalid RSS or Atom XML"):
        rss.run(ctx)


def test_rss_saves_healthy_feed_after_truncated_http_response(
    tmp_path: Path, monkeypatch
) -> None:
    real_fetch = rss.fetch_content

    def fetch(url: str):
        if url == "https://example.com/broken.xml":
            raise IncompleteRead(b"partial", 100)
        return real_fetch(url)

    monkeypatch.setattr(rss, "fetch_content", fetch)
    ctx = context(
        tmp_path,
        {
            "feeds": [
                {"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()},
                {"name": "broken", "url": "https://example.com/broken.xml"},
            ]
        },
    )

    with pytest.raises(RuntimeError, match="broken"):
        rss.run(ctx)

    saved = ctx.database.available_sources(
        since="",
        before="9999-12-31T23:59:59+00:00",
        excluded_ids=set(),
        limit=10,
    )
    assert len(saved) == 3


def test_rss_keeps_source_with_timestamp_outside_utc_range(tmp_path: Path, monkeypatch) -> None:
    document = b"""\
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>extreme-time</id>
        <title>Useful despite its timestamp</title>
        <link href="https://example.com/extreme-time" />
        <published>0001-01-01T00:00:00+14:00</published>
        <summary>Concrete details.</summary>
      </entry>
    </feed>
    """
    monkeypatch.setattr(
        rss,
        "fetch_content",
        lambda _url: (document, "https://example.com/feed.xml"),
    )
    ctx = context(
        tmp_path,
        {"feeds": [{"name": "time", "url": "https://example.com/feed.xml"}]},
    )

    result = rss.run(ctx)
    item = ctx.database.get_sources(["rss-time:extreme-time"])[0]

    assert result == {"job": "rss", "fetched": 1, "inserted": 1}
    assert item.source_published_at is None


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
        {
            "base_url": "https://hn.test",
            "limit": 1,
            "reader_url": "https://reader.test/api/",
        },
    )

    result = hn.run(ctx)
    item = ctx.database.get_sources(["hn:1"])[0]

    assert result == {"job": "hn", "fetched": 1, "inserted": 1, "reader_failures": 0}
    assert requested == ["https://reader.test/api/https://example.com/one"]
    assert item.content == "Concrete article facts."


def test_hn_reader_replaces_whitespace_story_text(tmp_path: Path, monkeypatch) -> None:
    payloads = {
        "https://hn.test/topstories.json": [1],
        "https://hn.test/item/1.json": {
            "id": 1,
            "type": "story",
            "title": "One",
            "url": "https://example.com/one",
            "text": "  \n",
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


def test_hn_reader_rejects_invalid_utf8(tmp_path: Path, monkeypatch) -> None:
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
        lambda url, **_kwargs: (b"\xff\xfe", url),
    )
    ctx = context(
        tmp_path,
        {"base_url": "https://hn.test", "limit": 1, "reader_url": "https://reader.test/"},
    )

    result = hn.run(ctx)

    assert result == {"job": "hn", "fetched": 0, "inserted": 0, "reader_failures": 1}
    assert ctx.database.status("hn:1") is None


def test_hn_reader_caps_article_content(tmp_path: Path, monkeypatch) -> None:
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
        lambda url, **_kwargs: (b"a" * (hn.MAX_READER_CHARS + 1), url),
    )
    ctx = context(
        tmp_path,
        {"base_url": "https://hn.test", "limit": 1, "reader_url": "https://reader.test/"},
    )

    hn.run(ctx)
    item = ctx.database.get_sources(["hn:1"])[0]

    assert len(item.content) == hn.MAX_READER_CHARS


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


def test_hugging_face_papers_collects_official_api_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        papers,
        "fetch_json",
        lambda _url: [
            {
                "paper": {
                    "id": "2607.26497",
                    "title": "A useful paper",
                    "summary": "A concrete abstract with measured results.",
                    "authors": [
                        {"name": "Ada"},
                        {"name": "Grace"},
                        {"name": "Linus"},
                        {"name": "Margaret"},
                        {"name": "Edsger"},
                        {"name": "Barbara"},
                        {"name": "Donald"},
                    ],
                    "upvotes": 42,
                    "githubRepo": "https://github.com/example/paper",
                    "projectPage": "https://example.com/project",
                }
            }
        ],
    )
    ctx = context(tmp_path, {"limit": 10, "sort": "trending"})
    object.__setattr__(ctx, "name", "papers")

    result = papers.run(ctx)
    item = ctx.database.get_sources(["hugging-face-papers:2607.26497"])[0]

    assert result == {"job": "papers", "fetched": 1, "inserted": 1}
    assert item.url == "https://arxiv.org/abs/2607.26497"
    assert item.author == "Ada, Grace, Linus, Margaret, Edsger, and 2 others"
    assert "42 Hugging Face upvotes" in item.content
    assert "https://github.com/example/paper" in item.content


def test_hugging_face_papers_rejects_bad_api_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(papers, "fetch_json", lambda _url: {"paper": []})
    ctx = context(tmp_path, {})

    with pytest.raises(ValueError, match="invalid paper list"):
        papers.run(ctx)


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


@pytest.mark.parametrize(
    "reader_url",
    ["https://reader.test/?token=x", "https://reader.test/#part", "https://:443"],
)
def test_hn_rejects_invalid_reader_url(
    tmp_path: Path, reader_url: str, monkeypatch
) -> None:
    monkeypatch.setattr(
        hn,
        "fetch_json",
        lambda _url: pytest.fail("invalid Reader URL should fail before collection"),
    )
    ctx = context(tmp_path, {"limit": 1, "reader_url": reader_url})

    with pytest.raises(ValueError, match="reader_url"):
        hn.run(ctx)


def test_hn_rejects_limit_above_documented_maximum(tmp_path: Path) -> None:
    ctx = context(tmp_path, {"limit": 501})

    with pytest.raises(ValueError, match="between 1 and 500"):
        hn.run(ctx)
