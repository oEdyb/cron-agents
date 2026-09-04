from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlencode

from cron_agents.db import Source, utc_now
from cron_agents.jobs import JobContext, fetch_json

API_BASE = "https://huggingface.co/api"
ALLOWED_KINDS = {"models", "spaces"}
ALLOWED_PARAM_KEYS = {"sort", "direction", "author", "search", "filter", "pipeline_tag"}
MAX_TAGS = 15
MAX_SHORT_FIELD = 100
MAX_TITLE_FIELD = 200


def run(ctx: JobContext) -> dict[str, object]:
    settings = ctx.job.settings
    limit = settings.get("limit_per_query", 20)
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("huggingface.limit_per_query must be a positive integer")
    queries = settings.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("huggingface.queries must be a non-empty list")

    fetched_at = utc_now()
    sources: list[Source] = []
    seen_hub_ids: set[str] = set()
    for query in queries:
        name, kind, params = _validate_query(query)
        query_params = dict(params)
        query_params["limit"] = limit
        url = f"{API_BASE}/{kind}?{urlencode(query_params)}"
        items = fetch_json(url)
        if not isinstance(items, list):
            raise ValueError(f"Hugging Face returned an invalid {kind} list for query {name!r}")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"Hugging Face returned an invalid {kind} record for {name!r}")
            hub_id = item.get("id")
            if not isinstance(hub_id, str) or not hub_id.strip():
                continue
            hub_id = hub_id.strip()
            if hub_id in seen_hub_ids:
                continue
            seen_hub_ids.add(hub_id)
            sources.append(
                _source(item, kind=kind, query_name=name, hub_id=hub_id, fetched_at=fetched_at)
            )

    updated = ctx.database.refresh_fetched_sources(sources)
    inserted = ctx.database.add_sources(sources)
    return {
        "job": ctx.name,
        "fetched": len(sources),
        "inserted": inserted,
        "updated": updated,
    }


def _validate_query(query: object) -> tuple[str, str, dict[str, object]]:
    if not isinstance(query, dict):
        raise ValueError("each huggingface query must be a mapping")
    name = query.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("each huggingface query needs a name")
    kind = query.get("kind")
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"huggingface query {name!r} kind must be models or spaces")
    params = query.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"huggingface query {name!r} params must be a mapping")
    unknown = sorted(set(params) - ALLOWED_PARAM_KEYS)
    if unknown:
        raise ValueError(
            f"huggingface query {name!r} has unsupported params: {', '.join(unknown)}"
        )
    for key, value in params.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(
                f"huggingface query {name!r} param {key!r} must be a string or integer"
            )
    return name, kind, params


def _cap(value: str, length: int = MAX_SHORT_FIELD) -> str:
    return value.strip()[:length]


def _published_at(item: dict[str, object]) -> str | None:
    for key in ("lastModified", "createdAt"):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat(timespec="seconds")
    return None


def _source(
    item: dict[str, object], *, kind: str, query_name: str, hub_id: str, fetched_at: str
) -> Source:
    if kind == "models":
        url = f"https://huggingface.co/{hub_id}"
    else:
        url = f"https://huggingface.co/spaces/{hub_id}"
    author = hub_id.split("/", 1)[0] if "/" in hub_id else None

    lines = [f"kind: {kind}"]

    pipeline_tag = item.get("pipeline_tag")
    if isinstance(pipeline_tag, str) and pipeline_tag.strip():
        lines.append(f"pipeline_tag: {_cap(pipeline_tag)}")

    for key in ("likes", "downloads", "trendingScore"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            lines.append(f"{key}: {value}")

    tags = item.get("tags")
    if isinstance(tags, list):
        clean_tags = [_cap(tag, 60) for tag in tags if isinstance(tag, str) and tag.strip()]
        if clean_tags:
            lines.append("tags: " + ", ".join(clean_tags[:MAX_TAGS]))

    if kind == "spaces":
        sdk = item.get("sdk")
        if isinstance(sdk, str) and sdk.strip():
            lines.append(f"sdk: {_cap(sdk, 40)}")
        card_data = item.get("cardData")
        if isinstance(card_data, dict):
            card_title = card_data.get("title")
            emoji = card_data.get("emoji")
            if isinstance(card_title, str) and card_title.strip():
                lines.append(f"card title: {_cap(card_title, 150)}")
            elif isinstance(emoji, str) and emoji.strip():
                lines.append(f"card emoji: {_cap(emoji, 10)}")

    last_modified = item.get("lastModified")
    if isinstance(last_modified, str) and last_modified.strip():
        lines.append(f"lastModified: {_cap(last_modified, 40)}")

    return Source.create(
        provider=f"hugging-face:{query_name}",
        provider_id=hub_id,
        url=url,
        title=_cap(hub_id, MAX_TITLE_FIELD),
        content="\n".join(lines),
        author=_cap(author) if author else None,
        fetched_at=fetched_at,
        source_published_at=_published_at(item),
    )
