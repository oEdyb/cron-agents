from __future__ import annotations

from cron_agents.db import Source, utc_now
from cron_agents.jobs import JobContext, fetch_json

# Official API: https://github.com/HackerNews/API
DEFAULT_BASE_URL = "https://hacker-news.firebaseio.com/v0"


def run(ctx: JobContext) -> dict[str, object]:
    limit = ctx.job.settings.get("limit", 30)
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("hn.limit must be between 1 and 500")
    base_url = ctx.job.settings.get("base_url", DEFAULT_BASE_URL)
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("hn.base_url must be a non-empty string")
    base_url = base_url.rstrip("/")

    story_ids = fetch_json(f"{base_url}/topstories.json")
    if not isinstance(story_ids, list):
        raise ValueError("Hacker News returned an invalid story list")

    fetched_at = utc_now()
    sources: list[Source] = []
    for story_id in story_ids[:limit]:
        if type(story_id) is not int:
            raise ValueError("Hacker News returned an invalid story ID")
        item = fetch_json(f"{base_url}/item/{story_id}.json")
        if not isinstance(item, dict) or item.get("type") != "story":
            continue
        if item.get("id") != story_id:
            raise ValueError(f"Hacker News item ID does not match {story_id}")
        title = item.get("title")
        if not isinstance(title, str) or not title or item.get("deleted") or item.get("dead"):
            continue
        url = item.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        if not isinstance(url, str):
            raise ValueError(f"Hacker News returned an invalid URL for {story_id}")
        sources.append(
            Source.create(
                provider="hn",
                provider_id=str(story_id),
                url=url,
                title=title,
                content=str(item.get("text") or ""),
                author=str(item.get("by") or "") or None,
                fetched_at=fetched_at,
            )
        )

    inserted = ctx.database.add_sources(sources)
    return {"job": "hn", "fetched": len(sources), "inserted": inserted}
