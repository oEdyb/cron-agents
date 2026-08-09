# cron-agents

Collect sources, let one agent choose what matters, and let a second agent write a daily briefing.

```text
collectors → SQLite → curator → selection.json → writer → briefing.md
```

The writer receives only the selected records. Saved selections and published source history prevent the same source from entering two briefings.

Collectors do not use a model. A new briefing normally makes two model calls: one for the curator and one for the writer.

## Set it up with a coding agent

Paste this prompt into Codex, Claude Code, Kimi, or another coding agent:

```text
Set up cron-agents from https://github.com/oEdyb/cron-agents on this machine.
Follow README.md and use Codex unless I ask for Kimi.
Ask me which sources and topics I care about.
Keep config.yaml and all credentials private.
Use the existing CLI and config without adding Docker or extra services.
Run the test suite, each collector, and one briefing.
Verify that the writer receives only the sources selected by the curator,
the source links work, and published sources cannot be selected again.
Do not schedule it until these checks pass.
Then ask what time I want it to run each day.
```

## Run your first briefing

You need macOS or Linux, Git, Python 3.12 or newer, and either Codex or Kimi Code CLI. The default config uses Codex.

### 1. Install Codex

Install Codex with OpenAI's installer:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Open a new terminal so it loads the installed command, then sign in:

```bash
codex login
codex login status
```

Skip the installer if `codex --version` already works.

### 2. Install cron-agents

Check that `python3 --version` reports 3.12 or newer. Install a current release from [python.org](https://www.python.org/downloads/) if needed. Then clone the repo and create its Python environment:

```bash
git clone https://github.com/oEdyb/cron-agents.git
cd cron-agents
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.example.yaml config.yaml
```

`config.yaml` is your private copy. Git ignores it.

### 3. Choose your sources and taste

The included feeds work without API keys. You can use them unchanged for the first run.

Open `config.yaml` when you want to make the briefing yours:

- Edit `jobs.briefing.context` to describe what you care about, what you know, and what the agents should skip
- Add RSS or Atom URLs under `jobs.rss.feeds`
- Add YouTube feeds with `https://www.youtube.com/feeds/videos.xml?channel_id=your_channel_id`

Find the existing `context: |` block inside `jobs.briefing`. Replace that block, including its indented text, with something like this:

```yaml
    context: |
      Audience: a developer who builds small AI tools.
      Goal: find ideas worth testing, building, or showing.
      Prefer visible demos, open projects, hard numbers, and strange failures.
      Skip routine releases, generic business news, and vague opinion posts.
```

### 4. Collect sources

Run each collector once:

```bash
.venv/bin/cron-agents run rss
.venv/bin/cron-agents run hn
.venv/bin/cron-agents run papers
.venv/bin/cron-agents run arxiv
```

Each command prints JSON and adds only new records to `data/state.db`.

### 5. Write the briefing

Run the curator and writer:

```bash
.venv/bin/cron-agents run briefing
```

Open `briefings/YYYY-MM-DD.md`. The curator chooses between one and ten sources. Each source link appears beside the text it supports.

## Use Kimi instead

Install Kimi Code CLI:

```bash
curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash
```

Open a new terminal so it loads the installed command, then sign in and enable tool-free agent files:

```bash
kimi login
export KIMI_CODE_EXPERIMENTAL_FLAG=1
```

Change both agent models in `config.yaml`:

```yaml
agents:
  curator:
    model: kimi
    prompt: prompts/curator.md
  writer:
    model: kimi
    prompt: prompts/writer.md
```

Kimi requires `KIMI_CODE_EXPERIMENTAL_FLAG=1` because this project uses a tool-free agent file. Keep that variable in the scheduled job too.

## Run it every day

Run the commands by hand before scheduling them. Log in to Codex or Kimi as the same Linux account that owns the schedule. On a headless server, use `codex login --device-auth` instead of `codex login`.

Open your crontab:

```bash
crontab -e
```

This example expects the repo at `/home/your_name/cron-agents`. Replace `your_name` and confirm that the `PATH` line contains the directory printed by `command -v codex` or `command -v kimi`.

```cron
PATH=/home/your_name/.local/bin:/home/your_name/.kimi-code/bin:/usr/local/bin:/usr/bin:/bin

0 */3 * * * cd /home/your_name/cron-agents && flock -n .run.lock .venv/bin/cron-agents run hn
30 */6 * * * cd /home/your_name/cron-agents && flock -n .run.lock .venv/bin/cron-agents run rss
45 6 * * * cd /home/your_name/cron-agents && flock -n .run.lock .venv/bin/cron-agents run papers
0 7 * * * cd /home/your_name/cron-agents && flock -n .run.lock .venv/bin/cron-agents run arxiv
0 21 * * * cd /home/your_name/cron-agents && flock -n .run.lock .venv/bin/cron-agents run briefing
```

Cron uses the server's timezone. If you use Kimi, add this line above the jobs:

```cron
KIMI_CODE_EXPERIMENTAL_FLAG=1
```

Use a dedicated Linux account on a server. Keep `config.yaml` untracked and keep CLI credentials in that account's home directory.

## Files created on your machine

Git ignores these paths:

```text
.venv/                          Python environment
cron_agents.egg-info/          local install metadata
config.yaml                     your feeds and briefing context
data/state.db                   source history and published status
data/selections/YYYY-MM-DD.json source reservation for one briefing
data/briefing.lock              briefing process lock
briefings/YYYY-MM-DD.md         finished briefing
```

Delete an unpublished selection file only when you mean to abandon that briefing and release its sources. Sources from a finished briefing stay blocked by SQLite history.

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

## Import private sources

Keep cookies and private collectors on their current machine. Send normalized JSON Lines (JSONL) records to the server:

```bash
private-x-command | ssh briefing-server '/srv/cron-agents/.venv/bin/cron-agents --config /srv/cron-agents/config.yaml import -'
```

Each line needs `provider`, `url`, and `title`. It may also contain `provider_id`, `content`, `author`, and `fetched_at`. Times must include a timezone, such as `2026-08-05T12:30:00+02:00`.

Import links from old briefings as published history so they cannot appear again:

```bash
.venv/bin/cron-agents import --published history.jsonl
```

The public core omits Reddit because its Data API requires approval and OAuth, plus removal of deleted user content.

## Fix common setup errors

- `model command not found`: run `command -v codex` or `command -v kimi`, then fix your shell or cron `PATH`
- Authentication error: run `codex login status`, `codex login`, or `kimi login` as the account that runs the job
- `briefing needs 1 sources`: run the collectors before the briefing
- Kimi rejects `--agent-file`: export `KIMI_CODE_EXPERIMENTAL_FLAG=1`

## Test the project

Install the development tools and run the checks:

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/pytest
```

The tests use local fixtures. They do not require a model login or network access.

## Official docs

- [Codex CLI setup](https://learn.chatgpt.com/docs/codex/cli), [authentication](https://learn.chatgpt.com/docs/auth), and [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Kimi Code CLI setup](https://moonshotai.github.io/kimi-code/en/guides/getting-started.html), [command options](https://moonshotai.github.io/kimi-code/en/reference/kimi-command), and [agent files](https://moonshotai.github.io/kimi-code/en/customization/agents.html)
- [Hugging Face Daily Papers](https://huggingface.co/docs/huggingface_hub/en/package_reference/hf_api#huggingface_hub.HfApi.list_daily_papers)
- [GitHub releases](https://docs.github.com/en/rest/releases/releases)
- [arXiv API](https://info.arxiv.org/help/api/user-manual.html)
- [Hacker News API](https://github.com/HackerNews/API)
- [Jina Reader](https://github.com/jina-ai/reader)
- [YouTube Atom feeds](https://developers.google.com/youtube/v3/guides/push_notifications)
- [RSS 2.0](https://www.rssboard.org/rss-specification) and [Atom](https://www.rfc-editor.org/rfc/rfc4287.html)
- [Reddit Data API rules](https://support.reddithelp.com/hc/en-us/articles/16160319875092-Reddit-Data-API-Wiki)
