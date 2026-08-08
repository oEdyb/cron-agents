# cron-agents

Collect sources on a schedule, let one agent choose what matters, and let a second agent write the daily briefing.

```text
collectors → SQLite → curator → selection.json → writer → briefing.md
```

The writer receives only the selected records. Saved selections and published source history prevent the same source from entering two briefings.

## Setup

Requires macOS or Linux, Python 3.12 or newer, and a logged-in Codex or Kimi CLI. Run it from a source checkout so the program can find its prompts and config.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml
```

The example config uses Codex. Change both agent models to `kimi` to use Kimi, then log in with a private Kimi home:

```bash
export KIMI_CODE_HOME="$HOME/.cron-agents-kimi"
export KIMI_CODE_EXPERIMENTAL_FLAG=1
kimi login
```

Keep those two variables in the scheduled job's environment too.

Model commands are plain argument lists. Prompts go to stdin unless the list contains `{prompt}`, which the runner replaces with the prompt for CLIs such as Kimi.

## Run

```bash
.venv/bin/cron-agents run rss
.venv/bin/cron-agents run hn
.venv/bin/cron-agents run arxiv
.venv/bin/cron-agents run papers
.venv/bin/cron-agents run briefing
```

The example HN job uses Jina Reader to turn each new linked article into text before curation. Remove `reader_url` to keep title-only HN records.

YouTube channels expose Atom feeds. Add their feed URLs to the `rss.feeds` list with the shared name `youtube`; the collector keeps the video ID, channel, description, and publish time without a login or API key.

The application rotates recent candidates across providers, so one busy feed cannot hide the rest.

The example allows 1–10 sources. The curator chooses the count from the evidence: one strong item on a quiet day or ten on a busy day. The writer gives major stories more space and keeps smaller signals compact.

Dates use UTC. `--date YYYY-MM-DD` retries or rebuilds one date. Historical runs only consider sources fetched before the end of that UTC date; writer retries reuse the saved selection.

## Schedule

The application runs one job and exits. Cron or systemd owns the schedule. These production examples target Linux; you can use macOS for local development and manual runs. Use one lock to serialize jobs:

```cron
0 */3 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run hn
30 */6 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run rss
45 6 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run papers
0 7 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run arxiv
0 21 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run briefing
```

Run it as a dedicated user. Keep `config.yaml` untracked and CLI credentials in that user's private home.

## Add a collector

Create a module with one `run(ctx)` function:

```python
from cron_agents.db import Source
from cron_agents.jobs import fetch_json


def run(ctx):
    items = fetch_json("https://example.com/items.json")
    sources = [
        Source.create(
            provider="example",
            provider_id=str(item["id"]),
            url=item["url"],
            title=item["title"],
            content=item.get("summary", ""),
        )
        for item in items
    ]
    inserted = ctx.database.add_sources(sources)
    return {"job": "example", "fetched": len(sources), "inserted": inserted}
```

Point a job at that module in `config.yaml`:

```yaml
jobs:
  example:
    module: my_jobs.example
```

Private collectors can send normalized JSONL without sharing their credentials with the server:

```bash
private-x-command | ssh briefing-server 'cron-agents --config /srv/cron-agents/config.yaml import -'
```

Each line needs `provider`, `url`, and `title`. It may also contain `provider_id`, `content`, `author`, and `fetched_at`. Times must include a timezone, such as `2026-08-05T12:30:00+02:00`. Run the X command on the machine that holds the cookies; only its source records cross SSH.

Use the same command with `--published` to seed links from old briefings. Those records stay in the ledger but never enter a new selection:

```bash
cron-agents import --published history.jsonl
```

The public core omits Reddit: its Data API requires approval and OAuth, plus removal of deleted user content.

## State

```text
data/state.db                    normalized sources and published status
data/selections/YYYY-MM-DD.json permanent source reservation for that briefing
data/briefing.lock               internal briefing serialization lock
briefings/YYYY-MM-DD.md         final briefing with clickable source links
```

Delete one selection file only when you intend to abandon that briefing and release its sources.

## Test

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
```

The tests use local fixtures and do not require a model login or network access.

## References

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command) and [agent files](https://moonshotai.github.io/kimi-code/en/customization/agents.html)
- [Hugging Face Daily Papers](https://huggingface.co/docs/huggingface_hub/en/package_reference/hf_api#huggingface_hub.HfApi.list_daily_papers)
- [GitHub releases](https://docs.github.com/en/rest/releases/releases)
- [arXiv API](https://info.arxiv.org/help/api/user-manual.html)
- [Hacker News API](https://github.com/HackerNews/API)
- [Jina Reader](https://github.com/jina-ai/reader)
- [YouTube Atom feeds](https://developers.google.com/youtube/v3/guides/push_notifications)
- [RSS 2.0](https://www.rssboard.org/rss-specification) and [Atom](https://www.rfc-editor.org/rfc/rfc4287.html)
- [Reddit Data API rules](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)
