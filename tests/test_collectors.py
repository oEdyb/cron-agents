from __future__ import annotations

import json
from datetime import date
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from cron_agents import jobs
from cron_agents.config import Config, JobConfig
from cron_agents.db import Database, Source
from cron_agents.jobs import JobContext, hn, huggingface, papers, rss

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


def test_rss_spaces_requests_to_the_same_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feed = (FIXTURES / "feed.xml").as_uri()
    ctx = context(
        tmp_path,
        {
            "host_interval_seconds": 20,
            "feeds": [
                {"name": "one", "url": "https://www.reddit.com/r/one/top/.rss?t=day"},
                {"name": "other", "url": "https://example.org/feed.xml"},
                {"name": "two", "url": "https://www.reddit.com/r/two/top/.rss?t=day"},
            ],
        },
    )
    clock = {"now": 100.0}
    sleeps: list[float] = []

    def fake_fetch(url: str) -> tuple[bytes, str]:
        clock["now"] += 1.0
        return (FIXTURES / "feed.xml").read_bytes(), feed

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(rss, "fetch_content", fake_fetch)
    monkeypatch.setattr(rss.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(rss.time, "sleep", fake_sleep)

    result = rss.run(ctx)

    assert result["fetched"] == 9
    assert len(sleeps) == 1
    assert 17.0 <= sleeps[0] <= 20.0


def test_rss_rejects_a_negative_host_interval(tmp_path: Path) -> None:
    ctx = context(
        tmp_path,
        {
            "host_interval_seconds": -1,
            "feeds": [{"name": "fixture", "url": (FIXTURES / "feed.xml").as_uri()}],
        },
    )

    with pytest.raises(ValueError, match="host_interval_seconds"):
        rss.run(ctx)


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


def test_fetch_sends_a_descriptive_user_agent(monkeypatch) -> None:
    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    captured: list[Request] = []

    def fake_urlopen(request, **_kwargs):
        captured.append(request)
        return Response(b"{}")

    monkeypatch.setattr(jobs, "urlopen", fake_urlopen)

    jobs.fetch_bytes("https://example.com/feed")

    assert captured[0].get_header("User-agent") == jobs.USER_AGENT
    assert captured[0].get_header("User-agent").startswith("cron-agents/")


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


@pytest.fixture
def one_paper_day(monkeypatch) -> None:
    monkeypatch.setattr(papers, "BACKFILL_DAYS", 1)


def test_hugging_face_papers_collects_official_daily_api_shape(
    tmp_path: Path, monkeypatch, one_paper_day
) -> None:
    requested: list[str] = []

    def fetch(url: str) -> list[dict[str, object]]:
        requested.append(url)
        return [
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
                    "submittedOnDailyAt": "2026-07-31T00:00:00.000Z",
                    "githubRepo": "https://github.com/example/paper",
                    "projectPage": "https://example.com/project",
                }
            }
        ]

    monkeypatch.setattr(
        papers,
        "fetch_json",
        fetch,
    )
    ctx = context(tmp_path, {})
    object.__setattr__(ctx, "name", "papers")

    result = papers.run(ctx)
    item = ctx.database.get_sources(["hugging-face-papers:2607.26497"])[0]

    assert result == {"job": "papers", "fetched": 1, "inserted": 1, "updated": 0}
    assert item.url == "https://arxiv.org/abs/2607.26497"
    assert item.author == "Ada, Grace, Linus, Margaret, Edsger, and 2 others"
    assert item.source_published_at == "2026-07-31T00:00:00+00:00"
    assert item.content.startswith(
        "42 Hugging Face upvotes.\nA concrete abstract with measured results."
    )
    assert "https://github.com/example/paper" in item.content
    query = parse_qs(urlsplit(requested[0]).query)
    assert query == {"date": ["2026-07-31"], "limit": ["100"], "p": ["0"]}


def test_hugging_face_papers_collects_every_daily_page(
    tmp_path: Path, monkeypatch, one_paper_day
) -> None:
    def item(number: int) -> dict[str, object]:
        return {
            "paper": {
                "id": f"2607.{number:05d}",
                "title": f"Paper {number}",
                "summary": f"Measured result {number} with enough detail.",
                "authors": [],
                "upvotes": 102 - number,
                "submittedOnDailyAt": "2026-07-31T00:00:00.000Z",
            }
        }

    pages = {0: [item(number) for number in range(100)], 1: [item(100), item(101)]}
    requested_pages: list[int] = []

    def fetch(url: str) -> list[dict[str, object]]:
        page = int(parse_qs(urlsplit(url).query)["p"][0])
        requested_pages.append(page)
        return pages[page]

    monkeypatch.setattr(papers, "fetch_json", fetch)
    ctx = context(tmp_path, {})
    object.__setattr__(ctx, "name", "papers")

    result = papers.run(ctx)

    assert result == {"job": "papers", "fetched": 102, "inserted": 102, "updated": 0}
    assert requested_pages == [0, 1]
    source_ids = [f"hugging-face-papers:2607.{number:05d}" for number in range(102)]
    assert len(ctx.database.get_sources(source_ids)) == 102


def test_hugging_face_papers_rechecks_the_last_seven_dates(
    tmp_path: Path, monkeypatch
) -> None:
    requested_dates: list[str] = []
    monkeypatch.setattr(papers, "utc_now", lambda: "2026-07-31T18:00:00+00:00")

    def fetch(url: str) -> list[dict[str, object]]:
        query = parse_qs(urlsplit(url).query)
        requested_date = query["date"][0]
        requested_dates.append(requested_date)
        if requested_date != "2026-07-25":
            return []
        return [
            {
                "paper": {
                    "id": "2607.26497",
                    "title": "A paper added late",
                    "summary": "A concrete abstract that appeared after the first daily pull.",
                    "authors": [],
                    "upvotes": 42,
                    "submittedOnDailyAt": "2026-07-25T00:00:00.000Z",
                }
            }
        ]

    monkeypatch.setattr(papers, "fetch_json", fetch)
    ctx = context(tmp_path, {})
    object.__setattr__(ctx, "name", "papers")

    result = papers.run(ctx)
    available = ctx.database.available_sources(
        since="2026-07-31T00:00:00+00:00",
        before="2026-08-01T00:00:00+00:00",
        excluded_ids=set(),
        limit=10,
    )

    assert result == {"job": "papers", "fetched": 1, "inserted": 1, "updated": 0}
    assert requested_dates == [
        "2026-07-31",
        "2026-07-30",
        "2026-07-29",
        "2026-07-28",
        "2026-07-27",
        "2026-07-26",
        "2026-07-25",
    ]
    assert [item.title for item in available] == ["A paper added late"]


def test_hugging_face_papers_rejects_item_outside_requested_day(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        papers,
        "fetch_json",
        lambda _url: [
            {
                "paper": {
                    "id": "2607.26497",
                    "title": "Old paper",
                    "summary": "This paper belongs to a different Daily Papers page.",
                    "authors": [],
                    "upvotes": 42,
                    "submittedOnDailyAt": "2026-07-30T00:00:00.000Z",
                }
            }
        ],
    )

    with pytest.raises(ValueError, match="outside requested date"):
        papers.run(context(tmp_path, {}))


def test_hugging_face_papers_enriches_an_existing_unpublished_arxiv_row(
    tmp_path: Path, monkeypatch, one_paper_day
) -> None:
    monkeypatch.setattr(papers, "utc_now", lambda: "2026-07-31T18:00:00+00:00")
    ctx = context(tmp_path, {})
    object.__setattr__(ctx, "name", "papers")
    existing = Source.create(
        provider="rss:arxiv",
        provider_id="2607.26497",
        url="https://arxiv.org/abs/2607.26497",
        title="A useful paper",
        content="The original arXiv abstract without Hugging Face votes.",
        fetched_at="2026-07-20T08:00:00+00:00",
        source_published_at="2026-07-20T00:00:00+00:00",
    )
    ctx.database.add_sources([existing])
    monkeypatch.setattr(
        papers,
        "fetch_json",
        lambda _url: [
            {
                "paper": {
                    "id": "2607.26497",
                    "title": "A useful paper",
                    "summary": "A concrete abstract with measured results.",
                    "authors": [{"name": "Ada"}],
                    "upvotes": 42,
                    "submittedOnDailyAt": "2026-07-31T00:00:00.000Z",
                }
            }
        ],
    )

    result = papers.run(ctx)
    saved = ctx.database.get_sources([existing.id])[0]
    available = ctx.database.available_sources(
        since="2026-07-31T00:00:00+00:00",
        before="2026-08-01T00:00:00+00:00",
        excluded_ids=set(),
        limit=10,
    )

    assert result == {"job": "papers", "fetched": 1, "inserted": 0, "updated": 1}
    assert saved.id == existing.id
    assert saved.provider == "rss:arxiv"
    assert saved.content.startswith("42 Hugging Face upvotes.")
    assert saved.source_published_at == "2026-07-31T00:00:00+00:00"
    assert saved.fingerprint != existing.fingerprint
    assert [source.id for source in available] == [existing.id]


def test_hugging_face_papers_does_not_revive_a_published_arxiv_row(
    tmp_path: Path, monkeypatch, one_paper_day
) -> None:
    ctx = context(tmp_path, {})
    object.__setattr__(ctx, "name", "papers")
    existing = Source.create(
        provider="rss:arxiv",
        provider_id="2607.26497",
        url="https://arxiv.org/abs/2607.26497",
        title="A useful paper",
        content="Already used.",
        fetched_at="2026-07-20T08:00:00+00:00",
        source_published_at="2026-07-20T00:00:00+00:00",
    )
    ctx.database.add_sources([existing], published=True)
    monkeypatch.setattr(
        papers,
        "fetch_json",
        lambda _url: [
            {
                "paper": {
                    "id": "2607.26497",
                    "title": "A useful paper",
                    "summary": "A concrete abstract with measured results.",
                    "authors": [{"name": "Ada"}],
                    "upvotes": 42,
                    "submittedOnDailyAt": "2026-07-31T00:00:00.000Z",
                }
            }
        ],
    )

    result = papers.run(ctx)
    saved = ctx.database.get_sources([existing.id])[0]

    assert result == {"job": "papers", "fetched": 1, "inserted": 0, "updated": 0}
    assert ctx.database.status(existing.id) == "published"
    assert saved.content == "Already used."
    assert saved.source_published_at == "2026-07-20T00:00:00+00:00"


@pytest.mark.parametrize("upvotes", [None, "42", True, -1])
def test_hugging_face_papers_rejects_invalid_upvotes(
    tmp_path: Path, monkeypatch, upvotes: object
) -> None:
    monkeypatch.setattr(
        papers,
        "fetch_json",
        lambda _url: [
            {
                "paper": {
                    "id": "2607.26497",
                    "title": "A useful paper",
                    "summary": "A concrete abstract with measured results.",
                    "authors": [],
                    "upvotes": upvotes,
                    "submittedOnDailyAt": "2026-07-31T00:00:00.000Z",
                }
            }
        ],
    )

    with pytest.raises(ValueError, match="invalid upvotes"):
        papers.run(context(tmp_path, {}))


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


HF_MODELS_FIXTURE = json.loads((FIXTURES / "huggingface-models.json").read_text())
HF_SPACES_FIXTURE = json.loads((FIXTURES / "huggingface-spaces.json").read_text())


def by_title(ctx: JobContext, title: str) -> Source:
    # Hugging Face hub IDs contain a slash, so Source.create hashes provider_id
    # into the stored ID. Look sources up by their stable title instead.
    sources = ctx.database.available_sources(
        since="", before="9999-12-31T23:59:59+00:00", excluded_ids=set(), limit=100
    )
    matches = [source for source in sources if source.title == title]
    assert len(matches) == 1, f"expected exactly one source titled {title!r}, found {matches}"
    return matches[0]


def test_huggingface_collects_models_and_spaces(tmp_path: Path, monkeypatch) -> None:
    requested: list[str] = []

    def fetch(url: str) -> list[dict[str, object]]:
        requested.append(url)
        if url.startswith("https://huggingface.co/api/models"):
            return HF_MODELS_FIXTURE
        if url.startswith("https://huggingface.co/api/spaces"):
            return HF_SPACES_FIXTURE
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(huggingface, "fetch_json", fetch)
    ctx = context(
        tmp_path,
        {
            "limit_per_query": 20,
            "queries": [
                {
                    "name": "trending-models",
                    "kind": "models",
                    "params": {"sort": "trendingScore", "direction": -1},
                },
                {
                    "name": "trending-spaces",
                    "kind": "spaces",
                    "params": {"sort": "trendingScore", "direction": -1},
                },
            ],
        },
    )
    object.__setattr__(ctx, "name", "huggingface")

    result = huggingface.run(ctx)

    assert result == {"job": "huggingface", "fetched": 4, "inserted": 4, "updated": 0}
    assert len(requested) == 2
    models_query = parse_qs(urlsplit(requested[0]).query)
    assert models_query == {"sort": ["trendingScore"], "direction": ["-1"], "limit": ["20"]}
    assert urlsplit(requested[0]).path == "/api/models"
    assert urlsplit(requested[1]).path == "/api/spaces"

    model = by_title(ctx, "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp")
    assert model.provider == "hugging-face:trending-models"
    assert model.title == "deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
    assert model.url == "https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-Vision-Exp"
    assert model.author == "deepseek-ai"
    assert "kind: models" in model.content
    assert "pipeline_tag: image-text-to-text" in model.content
    assert "likes: 590" in model.content
    assert "downloads: 133024" in model.content
    assert "trendingScore: 552" in model.content
    assert "tags: transformers, safetensors" in model.content
    assert model.source_published_at == "2026-08-31T06:16:18+00:00"

    space = by_title(ctx, "kulkas2pintu/wan555")
    assert space.provider == "hugging-face:trending-spaces"
    assert space.url == "https://huggingface.co/spaces/kulkas2pintu/wan555"
    assert space.author == "kulkas2pintu"
    assert "kind: spaces" in space.content
    assert "sdk: gradio" in space.content
    assert "card title: Wan 555 Video Generator" in space.content
    assert "lastModified: 2026-09-01T09:12:03.000Z" in space.content
    # Spaces without cardData or lastModified must not raise for missing optional fields.
    plain_space = by_title(ctx, "pollen-robotics/microduck-simulator")
    assert plain_space.source_published_at == "2026-08-20T07:53:36+00:00"
    assert "card title" not in plain_space.content
    assert "card emoji" not in plain_space.content


def test_huggingface_dedupes_hub_id_across_queries_first_query_wins(
    tmp_path: Path, monkeypatch
) -> None:
    shared = {
        "id": "unsloth/DeepSeek-V4-Flash-Vision-Exp-GGUF",
        "likes": 65,
        "downloads": 8679,
        "createdAt": "2026-08-31T10:44:53.000Z",
    }
    monkeypatch.setattr(huggingface, "fetch_json", lambda _url: [shared])
    ctx = context(
        tmp_path,
        {
            "limit_per_query": 20,
            "queries": [
                {"name": "unsloth", "kind": "models", "params": {}},
                {"name": "trending-models", "kind": "models", "params": {}},
            ],
        },
    )
    object.__setattr__(ctx, "name", "huggingface")

    result = huggingface.run(ctx)

    assert result == {"job": "huggingface", "fetched": 1, "inserted": 1, "updated": 0}
    source = by_title(ctx, "unsloth/DeepSeek-V4-Flash-Vision-Exp-GGUF")
    assert source.provider == "hugging-face:unsloth"


def test_huggingface_rejects_unknown_param_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        huggingface,
        "fetch_json",
        lambda _url: pytest.fail("an invalid query must fail before any fetch"),
    )
    ctx = context(
        tmp_path,
        {
            "queries": [
                {"name": "trending-models", "kind": "models", "params": {"full": "true"}},
            ]
        },
    )

    with pytest.raises(ValueError, match="unsupported params"):
        huggingface.run(ctx)


def test_huggingface_tolerates_missing_optional_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        huggingface,
        "fetch_json",
        lambda _url: [{"id": "someone/minimal-model"}],
    )
    ctx = context(
        tmp_path,
        {"queries": [{"name": "trending-models", "kind": "models", "params": {}}]},
    )
    object.__setattr__(ctx, "name", "huggingface")

    result = huggingface.run(ctx)
    source = by_title(ctx, "someone/minimal-model")

    assert result == {"job": "huggingface", "fetched": 1, "inserted": 1, "updated": 0}
    assert source.content == "kind: models"
    assert source.source_published_at is None


def test_huggingface_skips_record_missing_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        huggingface,
        "fetch_json",
        lambda _url: [{"likes": 5}, {"id": "someone/real-model"}],
    )
    ctx = context(
        tmp_path,
        {"queries": [{"name": "trending-models", "kind": "models", "params": {}}]},
    )
    object.__setattr__(ctx, "name", "huggingface")

    result = huggingface.run(ctx)

    assert result == {"job": "huggingface", "fetched": 1, "inserted": 1, "updated": 0}


def test_huggingface_rejects_bad_query_shape(tmp_path: Path) -> None:
    ctx = context(tmp_path, {"queries": [{"name": "x", "kind": "papers", "params": {}}]})

    with pytest.raises(ValueError, match="kind must be models or spaces"):
        huggingface.run(ctx)


def test_huggingface_rejects_empty_queries(tmp_path: Path) -> None:
    ctx = context(tmp_path, {"queries": []})

    with pytest.raises(ValueError, match="huggingface.queries"):
        huggingface.run(ctx)
