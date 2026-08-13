from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
import yaml
from conftest import read_log

from cron_agents.cli import run_job
from cron_agents.config import load_config
from cron_agents.db import Database, Source
from cron_agents.jobs import briefing
from cron_agents.model import ModelError


def add_sources(
    project,
    start: int,
    count: int,
    *,
    fetched_at: str = "2026-07-31T12:00:00+00:00",
) -> list[Source]:
    config = load_config(project.config)
    database = Database(config.state_dir / "state.db")
    database.initialize()
    sources = [
        Source.create(
            provider="test",
            provider_id=f"item-{number}",
            url=f"https://example.com/item-{number}",
            title=f"Source {number}",
            content=f"Detailed content for source {number}. " * 6,
            fetched_at=fetched_at,
        )
        for number in range(start, start + count)
    ]
    database.add_sources(sources)
    return sources


def x_source(provider_id: str, title: str, quoted: str | None = None) -> Source:
    lines = ["X following feed.", title]
    if quoted:
        lines.append(f"Quoted post: {quoted}")
    lines.append("Metrics: 20 likes, 500 views, 0 bookmarks.")
    return Source.create(
        provider="x",
        provider_id=provider_id,
        url=f"https://x.com/example/status/{provider_id}",
        title=title,
        content="\n".join(lines),
    )


def selection(project, run_date: str) -> dict[str, object]:
    path = project.root / "data" / "selections" / f"{run_date}.json"
    return json.loads(path.read_text())


def test_atomic_write_syncs_file_and_directory(tmp_path, monkeypatch) -> None:
    synced: list[bool] = []

    def record_fsync(file_descriptor: int) -> None:
        synced.append(stat.S_ISDIR(os.fstat(file_descriptor).st_mode))

    monkeypatch.setattr(briefing.os, "fsync", record_fsync)
    output = tmp_path / "nested" / "selection.json"

    briefing._atomic_write(output, "saved\n")

    assert output.read_text() == "saved\n"
    assert synced == [False, True]


def test_document_escapes_untrusted_source_metadata() -> None:
    source = Source.create(
        provider="test",
        provider_id="evil",
        url="https://example.com/a)><script>alert(1)</script>",
        title="Safe\n## Injected ![[Secret]] [source:unknown] [break](https://evil)",
    )

    document = briefing._document(
        "2026-08-01",
        [source],
        "Body [source:test:evil]",
    )

    assert "\n## Injected" not in document
    assert "![[Secret]]" not in document
    assert "[source:unknown]" not in document
    assert "<script>" not in document
    assert 'summary: "Curated daily briefing from 1 selected source."' in document
    assert (
        "[Source](https://example.com/a%29%3E%3Cscript%3Ealert%281%29%3C/script%3E)"
        in document
    )
    assert "## Sources" not in document
    assert "[source:test:evil]" not in document


def test_writer_receives_only_selected_sources(project) -> None:
    sources = add_sources(project, 1, 3)

    result = run_job(project.config, "briefing", date(2026, 7, 31))

    selected = selection(project, "2026-07-31")["source_ids"]
    output = (project.root / "briefings" / "2026-07-31.md").read_text()
    events = read_log(project.log)
    writer_prompt = events[1]["prompt"]
    editor_event = events[2]
    editor_prompt = editor_event["prompt"]
    rejected = ({f"test:item-{number}" for number in range(1, 4)} - set(selected)).pop()
    assert result["recovered"] is False
    assert len(selected) == 2
    assert "[source:" not in output
    assert output.count("[Source](") == len(selected)
    assert all(f'  - "{source_id}"' in output for source_id in selected)
    assert "type: briefing" in output
    assert 'title: "Daily Briefing — 2026-07-31"' in output
    assert 'summary: "Curated daily briefing from 2 selected sources."' in output
    assert "status: generated" in output
    assert "tags: [briefing]" in output
    assert "## Sources" not in output
    assert all(f"]({source.url})" in output for source in sources if source.id in selected)
    assert "Briefing date: 2026-07-31" in writer_prompt
    assert "Use web search and direct page fetching" in writer_prompt
    assert all(source_id in writer_prompt for source_id in selected)
    assert {record["id"] for record in editor_event["sources"]} == set(selected)
    assert rejected not in writer_prompt
    assert rejected not in json.dumps(editor_event["sources"])
    assert "The briefing is finished only when:" in editor_prompt
    assert "lowest missing foundation" in editor_prompt
    assert "include only the steps needed to understand it" in editor_prompt
    assert "what happened and why it matters" in editor_prompt
    assert "a concrete example when it makes the idea easier" in editor_prompt
    assert "does not force an example, visible labels, or the same layout" in editor_prompt
    assert "every story has visible **What happened:**" not in editor_prompt
    assert "one or two sentences per label" not in editor_prompt
    assert "do not force every causal step or alternative into every story" in editor_prompt
    assert (
        "an alternative only when it changes the reader's understanding or choice" in editor_prompt
    )
    assert "technical names after the things they name make sense" in editor_prompt
    assert "not an information limit" in editor_prompt
    assert "the draft missed" in editor_prompt
    assert "Open draft.md and sources.json" in editor_prompt
    assert "DRAFT_BRIEFING=" not in editor_prompt
    assert "Clear because" in output
    assert [event["kind"] for event in events] == ["curator", "writer", "editor"]


def test_editor_receives_full_selected_content(project) -> None:
    sources = add_sources(project, 1, 3)
    long_content = "start " + ("middle " * 300) + "full-record-ending"
    config = load_config(project.config)
    database = Database(config.state_dir / "state.db")
    database.initialize()
    database.refresh_fetched_sources(
        [Source.create(
            provider=sources[0].provider,
            provider_id=sources[0].provider_id,
            url=sources[0].url,
            title=sources[0].title,
            content=long_content,
            fetched_at=sources[0].fetched_at,
        )]
    )
    project.data["jobs"]["briefing"]["max_content_chars"] = 100
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))

    run_job(project.config, "briefing", date(2026, 7, 31))

    writer_prompt = read_log(project.log)[1]["prompt"]
    editor_event = read_log(project.log)[2]
    assert "full-record-ending" not in writer_prompt
    assert "full-record-ending" not in editor_event["prompt"]
    assert "full-record-ending" in json.dumps(editor_event["sources"])


def test_default_selection_has_no_fixed_maximum(project) -> None:
    project.data["jobs"]["briefing"].pop("min_sources")
    project.data["jobs"]["briefing"].pop("max_sources")
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))
    add_sources(project, 1, 10)

    result = run_job(project.config, "briefing", date(2026, 7, 31))

    curator_prompt = read_log(project.log)[0]["prompt"]
    assert result["sources"] == 2
    assert "Select at least 1 source ID." in curator_prompt
    assert "There is no target count" in curator_prompt
    assert "Select between" not in curator_prompt


def test_default_selection_accepts_more_than_ten_sources(project, monkeypatch) -> None:
    project.data["jobs"]["briefing"].pop("max_sources")
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))
    monkeypatch.setenv("MODEL_SELECT_ALL", "1")
    add_sources(project, 1, 12)

    result = run_job(project.config, "briefing", date(2026, 7, 31))

    assert result["sources"] == 12
    assert len(selection(project, "2026-07-31")["source_ids"]) == 12


def test_explicit_maximum_is_still_enforced(project, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_SELECT_ALL", "1")
    add_sources(project, 1, 3)

    with pytest.raises(ValueError, match="curator must select at most 2 sources"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    curator_prompt = read_log(project.log)[0]["prompt"]
    assert "Select between 2 and 2 source IDs." in curator_prompt


def test_default_candidate_pool_is_not_cut_to_the_newest_sixty(project) -> None:
    add_sources(project, 1, 125)

    run_job(project.config, "briefing", date(2026, 7, 31))

    prompt = read_log(project.log)[0]["prompt"]
    records_json = prompt.split("CANDIDATE_SOURCES=", 1)[1]
    records = json.loads(records_json)
    assert len(records) == 125
    assert records_json.startswith('[{"id":')


def test_x_prompt_record_does_not_repeat_the_post_text() -> None:
    source = x_source(
        "123",
        "Claude Code can control an iPhone",
        "No jailbreak is needed.",
    )

    record = source.prompt_record(5000)

    assert record["content"] == (
        "X following feed.\n"
        "Quoted post: No jailbreak is needed.\n"
        "Metrics: 20 likes, 500 views, 0 bookmarks."
    )


def test_curator_skips_x_posts_without_enough_text() -> None:
    noise = [
        x_source("link", "https://t.co/example"),
        x_source("reaction", "Literally"),
        x_source("short-1", "This is wild"),
        x_source("short-2", "Could not agree more"),
        x_source("short-3", "Wow this changes everything"),
        x_source("trivial-quote", "So true", "Exactly"),
    ]
    useful_quote = x_source(
        "quote", "Wow", "Claude Code can control an iPhone without a jailbreak."
    )
    useful = x_source("useful", "Claude Code can control an iPhone without a jailbreak")

    candidates = briefing._useful_candidates([*noise, useful_quote, useful])

    assert [source.id for source in candidates] == [useful_quote.id, useful.id]


def test_writer_retry_reuses_selection(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_FAIL_WRITER", "1")

    with pytest.raises(ModelError, match="exited 9"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    first_selection = selection(project, "2026-07-31")
    assert not (project.root / "briefings" / "2026-07-31.md").exists()
    output_dir = project.root / "briefings"
    if output_dir.exists():
        assert not list(output_dir.glob(".*"))

    monkeypatch.delenv("MODEL_FAIL_WRITER")
    result = run_job(project.config, "briefing", date(2026, 7, 31))

    assert selection(project, "2026-07-31") == first_selection
    assert result["recovered"] is False
    assert [event["kind"] for event in read_log(project.log)] == [
        "curator",
        "writer",
        "writer",
        "editor",
    ]


def test_editor_failure_keeps_selection_for_retry(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_FAIL_EDITOR", "1")

    with pytest.raises(ModelError, match="exited 10"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    first_selection = selection(project, "2026-07-31")
    assert not (project.root / "briefings" / "2026-07-31.md").exists()

    monkeypatch.delenv("MODEL_FAIL_EDITOR")
    result = run_job(project.config, "briefing", date(2026, 7, 31))

    assert selection(project, "2026-07-31") == first_selection
    assert result["recovered"] is False
    assert [event["kind"] for event in read_log(project.log)] == [
        "curator",
        "writer",
        "editor",
        "writer",
        "editor",
    ]
    assert not list((project.root / "data").glob("briefing-*-*"))


def test_editor_cannot_publish_an_unknown_citation(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_EDITOR_UNKNOWN_CITATION", "1")

    with pytest.raises(ValueError, match="unselected source IDs"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert selection(project, "2026-07-31")["source_ids"]
    assert not (project.root / "briefings" / "2026-07-31.md").exists()
    assert not list((project.root / "data").glob("briefing-*-*"))


def test_editor_cannot_drop_a_selected_citation(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_EDITOR_DROP_CITATION", "1")

    with pytest.raises(ValueError, match="omitted source IDs"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert not (project.root / "briefings" / "2026-07-31.md").exists()


def test_editor_must_confirm_completion(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_EDITOR_NO_COMPLETE", "1")

    with pytest.raises(ValueError, match="did not confirm completion"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert not (project.root / "briefings" / "2026-07-31.md").exists()


def test_config_without_editor_keeps_two_agent_flow(project) -> None:
    add_sources(project, 1, 3)
    project.data["jobs"]["briefing"].pop("editor")
    project.data["agents"].pop("editor")
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))

    result = run_job(project.config, "briefing", date(2026, 7, 31))

    output = (project.root / "briefings" / "2026-07-31.md").read_text()
    assert result["recovered"] is False
    assert [event["kind"] for event in read_log(project.log)] == ["curator", "writer"]
    assert "Useful because" in output


def test_editor_must_use_the_writer_prompt(project) -> None:
    add_sources(project, 1, 3)
    other_prompt = project.root / "prompts" / "other.md"
    other_prompt.write_text("Different instructions.")
    project.data["agents"]["editor"]["prompt"] = "prompts/other.md"
    project.config.write_text(yaml.safe_dump(project.data, sort_keys=False))

    with pytest.raises(ValueError, match="must use the same prompt"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert not (project.root / "briefings" / "2026-07-31.md").exists()


def test_saved_selection_reserves_sources_for_later_dates(project, monkeypatch) -> None:
    first_sources = add_sources(
        project,
        1,
        3,
        fetched_at="2026-07-30T12:00:00+00:00",
    )
    monkeypatch.setenv("MODEL_FAIL_WRITER", "1")
    with pytest.raises(ModelError):
        run_job(project.config, "briefing", date(2026, 7, 30))
    first_ids = set(selection(project, "2026-07-30")["source_ids"])
    assert first_ids <= {source.id for source in first_sources}

    monkeypatch.delenv("MODEL_FAIL_WRITER")
    later_sources = add_sources(project, 4, 3)
    run_job(project.config, "briefing", date(2026, 7, 31))
    later_ids = set(selection(project, "2026-07-31")["source_ids"])

    assert later_ids.isdisjoint(first_ids)
    assert later_ids <= {source.id for source in later_sources} | {
        source.id for source in first_sources
    }


def test_published_sources_are_not_selected_again(project) -> None:
    add_sources(
        project,
        1,
        5,
        fetched_at="2026-07-30T12:00:00+00:00",
    )

    run_job(project.config, "briefing", date(2026, 7, 30))
    run_job(project.config, "briefing", date(2026, 7, 31))

    first_ids = set(selection(project, "2026-07-30")["source_ids"])
    later_ids = set(selection(project, "2026-07-31")["source_ids"])
    assert first_ids.isdisjoint(later_ids)


def test_concurrent_briefings_cannot_reserve_the_same_source(project) -> None:
    add_sources(
        project,
        1,
        5,
        fetched_at="2026-07-30T12:00:00+00:00",
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_job, project.config, "briefing", date(2026, 7, 30)),
            pool.submit(run_job, project.config, "briefing", date(2026, 7, 31)),
        ]
        for future in futures:
            future.result()

    first_ids = set(selection(project, "2026-07-30")["source_ids"])
    later_ids = set(selection(project, "2026-07-31")["source_ids"])
    assert first_ids.isdisjoint(later_ids)


def test_historical_briefing_excludes_sources_fetched_later(project) -> None:
    add_sources(
        project,
        1,
        2,
        fetched_at="2026-07-30T12:00:00+00:00",
    )
    later = add_sources(project, 3, 2)

    run_job(project.config, "briefing", date(2026, 7, 30))

    selected = set(selection(project, "2026-07-30")["source_ids"])
    assert selected.isdisjoint(source.id for source in later)


def test_new_selection_cannot_be_created_for_a_future_date(project) -> None:
    add_sources(project, 1, 3)

    with pytest.raises(ValueError, match="future date"):
        run_job(project.config, "briefing", date(2026, 8, 1))

    assert not project.log.exists()


def test_invalid_curator_cannot_create_selection(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_INVALID_CURATOR", "1")

    with pytest.raises(ValueError, match="unknown source IDs"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    selection_dir = project.root / "data" / "selections"
    assert not selection_dir.exists()


def test_unknown_writer_citation_keeps_selection_for_retry(project, monkeypatch) -> None:
    add_sources(project, 1, 3)
    monkeypatch.setenv("MODEL_UNKNOWN_CITATION", "1")

    with pytest.raises(ValueError, match="unselected source IDs"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert selection(project, "2026-07-31")["source_ids"]
    assert not (project.root / "briefings" / "2026-07-31.md").exists()


def test_existing_output_recovers_publication_without_agents(project) -> None:
    sources = add_sources(project, 1, 2)
    selection_dir = project.root / "data" / "selections"
    selection_dir.mkdir(parents=True)
    selection_dir.joinpath("2026-07-31.json").write_text(
        json.dumps(
            {
                "version": 1,
                "date": "2026-07-31",
                "created_at": "2026-07-31T20:00:00+00:00",
                "source_ids": [source.id for source in sources],
            }
        )
    )
    output_dir = project.root / "briefings"
    output_dir.mkdir()
    output_dir.joinpath("2026-07-31.md").write_text("existing")

    result = run_job(project.config, "briefing", date(2026, 7, 31))
    database = Database(project.root / "data" / "state.db")

    assert result["recovered"] is True
    assert not project.log.exists()
    assert all(database.status(source.id) == "published" for source in sources)


def test_output_without_selection_is_not_repaired_with_new_sources(project) -> None:
    add_sources(project, 1, 3)
    output_dir = project.root / "briefings"
    output_dir.mkdir()
    output_dir.joinpath("2026-07-31.md").write_text("orphaned")

    with pytest.raises(ValueError, match="output exists without its selection"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert not project.log.exists()


def test_output_path_must_be_a_file(project) -> None:
    add_sources(project, 1, 3)
    output = project.root / "briefings" / "2026-07-31.md"
    output.mkdir(parents=True)

    with pytest.raises(ValueError, match="output is not a file"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert not project.log.exists()


def test_malformed_reservation_blocks_new_curation(project) -> None:
    add_sources(project, 1, 3)
    selection_dir = project.root / "data" / "selections"
    selection_dir.mkdir(parents=True)
    selection_dir.joinpath("2026-07-30.json").write_text("not json")

    with pytest.raises(ValueError, match="invalid selection file"):
        run_job(project.config, "briefing", date(2026, 7, 31))

    assert not project.log.exists()
