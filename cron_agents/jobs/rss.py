from __future__ import annotations

from urllib.parse import urljoin
from xml.etree import ElementTree

from cron_agents.db import Source, utc_now
from cron_agents.jobs import JobContext, fetch_content

# Formats: https://www.rssboard.org/rss-specification and RFC 4287.
XML_BASE = "{http://www.w3.org/XML/1998/namespace}base"


def _name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ElementTree.Element, *names: str) -> ElementTree.Element | None:
    wanted = set(names)
    return next((child for child in element if _name(child.tag) in wanted), None)


def _text(element: ElementTree.Element, *names: str) -> str:
    child = _child(element, *names)
    return "" if child is None else "".join(child.itertext()).strip()


def _link(element: ElementTree.Element, base_url: str) -> str:
    for child in element:
        if _name(child.tag) != "link":
            continue
        child_base = urljoin(base_url, child.attrib.get(XML_BASE, ""))
        href = child.attrib.get("href")
        relation = child.attrib.get("rel", "alternate")
        if href and relation == "alternate":
            return urljoin(child_base, href)
        if child.text and child.text.strip():
            return urljoin(child_base, child.text.strip())
    guid = _child(element, "guid")
    if guid is not None and guid.attrib.get("isPermaLink", "true").lower() == "true":
        guid_base = urljoin(base_url, guid.attrib.get(XML_BASE, ""))
        return "" if guid.text is None else urljoin(guid_base, guid.text.strip())
    return ""


def _entries(document: bytes, base_url: str) -> list[tuple[ElementTree.Element, str]]:
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as error:
        raise ValueError("invalid RSS or Atom XML") from error
    entry_name = "entry" if _name(root.tag) == "feed" else "item"
    entries: list[tuple[ElementTree.Element, str]] = []

    def visit(element: ElementTree.Element, parent_base: str) -> None:
        element_base = urljoin(parent_base, element.attrib.get(XML_BASE, ""))
        if _name(element.tag) == entry_name:
            entries.append((element, element_base))
            return
        for child in element:
            visit(child, element_base)

    visit(root, base_url)
    return entries


def run(ctx: JobContext) -> dict[str, object]:
    feeds = ctx.job.settings.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise ValueError("rss.feeds must be a non-empty list")
    limit = ctx.job.settings.get("limit_per_feed", 20)
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("rss.limit_per_feed must be a positive integer")

    fetched_at = utc_now()
    sources: list[Source] = []
    for feed in feeds:
        if not isinstance(feed, dict):
            raise ValueError("each RSS feed must be a mapping")
        name = feed.get("name")
        url = feed.get("url")
        if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
            raise ValueError("each RSS feed needs name and url")

        document, document_url = fetch_content(url)
        for entry, entry_base in _entries(document, document_url)[:limit]:
            title = _text(entry, "title")
            link = _link(entry, entry_base)
            if not title or not link:
                continue
            provider_id = _text(entry, "guid", "id") or None
            content = _text(entry, "description", "summary", "content", "encoded")
            author = _text(entry, "author", "creator") or None
            sources.append(
                Source.create(
                    provider=f"rss:{name}",
                    provider_id=provider_id,
                    url=link,
                    title=title,
                    content=content,
                    author=author,
                    fetched_at=fetched_at,
                )
            )

    inserted = ctx.database.add_sources(sources)
    return {"job": ctx.name, "fetched": len(sources), "inserted": inserted}
