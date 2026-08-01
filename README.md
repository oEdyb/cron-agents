# cron-agents

Collect sources on a schedule, let one agent choose what matters, and let a second agent write the daily briefing.

```text
collectors → SQLite → curator → selection.json → writer → briefing.md
```

The writer receives only the selected records. Saved selections and published source history prevent the same source from entering two briefings.

## Setup

Requires macOS or Linux, Python 3.12 or newer, and a logged-in Codex or Kimi CLI. Run it from a source checkout; the prompts and example config are intentionally not bundled into a standalone wheel.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml
```

The example config uses Codex. To use Kimi, change the `curator` and `writer` model to `kimi`, then log in with a private Kimi home:

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
.venv/bin/cron-agents run briefing
```

Dates use UTC. `--date YYYY-MM-DD` retries or rebuilds one date. Historical runs only consider sources fetched before the end of that UTC date; writer retries reuse the saved selection.

## Schedule

The application runs one job and exits. Cron or systemd owns the schedule. These production examples target Linux; macOS is supported for local development and manual runs. Use one lock to serialize jobs:

```cron
0 */3 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run hn
30 */6 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run rss
0 21 * * * cd /srv/cron-agents && flock -n .run.lock .venv/bin/cron-agents run briefing
```

Run it as a dedicated user. Keep `config.yaml` untracked and CLI credentials in that user's private home.

For a VPS deployment with model authentication, systemd timers, and output-only sync, read [DEPLOY.md](DEPLOY.md) before changing the host.

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

X works through the same contract, but its account cookies belong in a private deployment module. Reddit is also left out of the public core: its Data API requires approval and OAuth, plus removal of deleted user content.

## State

```text
data/state.db                    normalized sources and published status
data/selections/YYYY-MM-DD.json permanent source reservation for that briefing
data/briefing.lock               internal briefing serialization lock
briefings/YYYY-MM-DD.md         final briefing with selected source IDs
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

- [Codex authentication](https://learn.chatgpt.com/docs/auth) and [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Kimi command](https://moonshotai.github.io/kimi-code/en/reference/kimi-command) and [agent files](https://moonshotai.github.io/kimi-code/en/customization/agents.html)
- [Hacker News API](https://github.com/HackerNews/API)
- [RSS 2.0](https://www.rssboard.org/rss-specification) and [Atom](https://www.rfc-editor.org/rfc/rfc4287.html)
- [Reddit Data API rules](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)
