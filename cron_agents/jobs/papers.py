from __future__ import annotations

from urllib.parse import quote, urlencode

from cron_agents.db import Source, utc_now
from cron_agents.jobs import JobContext, fetch_json

API_URL = "https://huggingface.co/api/daily_papers"


def run(ctx: JobContext) -> dict[str, object]:
    limit = ctx.job.settings.get("limit", 30)
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("papers.limit must be between 1 and 100")
    sort = ctx.job.settings.get("sort", "trending")
    if sort not in {"publishedAt", "trending"}:
        raise ValueError("papers.sort must be publishedAt or trending")

    items = fetch_json(f"{API_URL}?{urlencode({'limit': limit, 'sort': sort})}")
    if not isinstance(items, list):
        raise ValueError("Hugging Face returned an invalid paper list")

    fetched_at = utc_now()
    sources = [_source(item, fetched_at, index) for index, item in enumerate(items, 1)]
    inserted = ctx.database.add_sources(sources)
    return {"job": ctx.name, "fetched": len(sources), "inserted": inserted}


def _source(item: object, fetched_at: str, index: int) -> Source:
    if not isinstance(item, dict) or not isinstance(item.get("paper"), dict):
        raise ValueError(f"Hugging Face paper {index} has an invalid shape")
    paper = item["paper"]
    paper_id = paper.get("id")
    title = paper.get("title")
    summary = paper.get("summary")
    if not all(isinstance(value, str) and value.strip() for value in (paper_id, title, summary)):
        raise ValueError(f"Hugging Face paper {index} is missing id, title, or summary")

    authors = paper.get("authors", [])
    if not isinstance(authors, list):
        raise ValueError(f"Hugging Face paper {index} has invalid authors")
    author_names = [
        author["name"].strip()[:100]
        for author in authors
        if isinstance(author, dict)
        and isinstance(author.get("name"), str)
        and author["name"].strip()
    ]

    details = [summary.strip()]
    upvotes = paper.get("upvotes")
    if isinstance(upvotes, int | float) and not isinstance(upvotes, bool):
        details.append(f"{upvotes:g} Hugging Face upvotes.")
    for label, field in (("Code", "githubRepo"), ("Project", "projectPage")):
        value = paper.get(field)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            details.append(f"{label}: {value}")

    author = ", ".join(author_names[:5])
    if len(author_names) > 5:
        author = f"{author}, and {len(author_names) - 5} others"

    return Source.create(
        provider="hugging-face-papers",
        provider_id=paper_id,
        url=f"https://arxiv.org/abs/{quote(paper_id, safe='/.')}",
        title=title,
        content="\n".join(details),
        author=author or None,
        fetched_at=fetched_at,
    )
