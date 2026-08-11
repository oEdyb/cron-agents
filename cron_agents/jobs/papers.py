from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from cron_agents.db import Source, utc_now
from cron_agents.jobs import JobContext, fetch_json

API_URL = "https://huggingface.co/api/daily_papers"
PAGE_SIZE = 100
MAX_PAGES = 100


def run(ctx: JobContext) -> dict[str, object]:
    requested_date = ctx.date.isoformat()
    fetched_at = utc_now()
    sources: list[Source] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        url = f"{API_URL}?{urlencode({'date': requested_date, 'limit': PAGE_SIZE, 'p': page})}"
        items = fetch_json(url)
        if not isinstance(items, list):
            raise ValueError("Hugging Face returned an invalid paper list")
        for item in items:
            source = _source(item, fetched_at, len(sources) + 1, requested_date)
            if source.id not in seen:
                seen.add(source.id)
                sources.append(source)
        if len(items) < PAGE_SIZE:
            break
    else:
        raise ValueError("Hugging Face returned too many paper pages")

    updated = ctx.database.refresh_fetched_sources(sources)
    inserted = ctx.database.add_sources(sources)
    return {
        "job": ctx.name,
        "fetched": len(sources),
        "inserted": inserted,
        "updated": updated,
    }


def _source(item: object, fetched_at: str, index: int, requested_date: str) -> Source:
    if not isinstance(item, dict) or not isinstance(item.get("paper"), dict):
        raise ValueError(f"Hugging Face paper {index} has an invalid shape")
    paper = item["paper"]
    paper_id = paper.get("id")
    title = paper.get("title")
    summary = paper.get("summary")
    if not all(isinstance(value, str) and value.strip() for value in (paper_id, title, summary)):
        raise ValueError(f"Hugging Face paper {index} is missing id, title, or summary")
    submitted = paper.get("submittedOnDailyAt")
    if not isinstance(submitted, str):
        raise ValueError(f"Hugging Face paper {index} is missing its Daily Papers date")
    try:
        submitted_at = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Hugging Face paper {index} has an invalid Daily Papers date") from error
    if submitted_at.date().isoformat() != requested_date:
        raise ValueError(f"Hugging Face paper {index} is outside requested date")
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=UTC)

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

    upvotes = paper.get("upvotes")
    if not isinstance(upvotes, int) or isinstance(upvotes, bool) or upvotes < 0:
        raise ValueError(f"Hugging Face paper {index} has invalid upvotes")
    details = [f"{upvotes} Hugging Face upvotes.", summary.strip()]
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
        source_published_at=submitted_at.astimezone(UTC).isoformat(timespec="seconds"),
    )
