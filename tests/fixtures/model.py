from __future__ import annotations

import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
if "BRIEFING_SECTIONS=" in prompt:
    kind = "reader-check"
elif "SOURCE_RECORDS=" in prompt:
    kind = "reader-cards"
elif "SELECTED_SOURCE_CARDS=" in prompt:
    kind = "grouper"
elif "CANDIDATE_SOURCES=" in prompt:
    kind = "curator"
else:
    kind = "writer"

event: dict[str, object] = {"kind": kind, "prompt": prompt}
if log_path := os.environ.get("MODEL_LOG"):
    with Path(log_path).open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")

if kind == "reader-cards":
    records = json.loads(prompt.split("SOURCE_RECORDS=", 1)[1])
    if os.environ.get("MODEL_READER_DROP_CARD") and len(records) > 1:
        records = records[:-1]
    if os.environ.get("MODEL_READER_UNKNOWN_CARD") and len(records) > 1:
        records[-1]["id"] = "rss-arxiv:204795ac0a186819eb0b270d"
    label = "CARD" if os.environ.get("MODEL_READER_BAD_LABEL") else "KEEP"
    skip_first = bool(os.environ.get("MODEL_READER_SKIP_CARD"))
    cards = [
        {
            "id": record["id"],
            "card": (
                f"{'SKIP' if skip_first and position == 0 else label}: "
                f"{record['title']} asks a concrete question and reports useful "
                "evidence that can be learned from."
            ),
        }
        for position, record in enumerate(records)
    ]
    if os.environ.get("MODEL_READER_MALFORMED_CARD") and len(cards) > 1:
        cards[1] = {"id": records[1]["id"], "summary": cards[1]["card"]}
    print(json.dumps({"cards": cards}))
elif kind == "reader-check":
    records_json, sections_json = prompt.split("SOURCE_RECORDS=", 1)[1].split(
        "\n\nBRIEFING_SECTIONS=", 1
    )
    records = json.loads(records_json)
    sections = json.loads(sections_json)
    source_ids = []
    for section in sections:
        matches = [
            record["id"] for record in records if record["title"] in section["heading"].split(" + ")
        ]
        source_ids.append(matches)
    checker_calls = 0
    if log_path := os.environ.get("MODEL_LOG"):
        checker_calls = sum(
            json.loads(line).get("kind") == "reader-check"
            for line in Path(log_path).read_text().splitlines()
        )
    if (
        os.environ.get("MODEL_CHECKER_SWAP")
        or (os.environ.get("MODEL_CHECKER_SWAP_ONCE") and checker_calls == 1)
    ) and len(source_ids) > 1:
        source_ids[0], source_ids[1] = source_ids[1], source_ids[0]
    print(
        json.dumps(
            {
                "sections": [
                    {"section": section["section"], "source_ids": ids}
                    for section, ids in zip(sections, source_ids, strict=True)
                ]
            }
        )
    )
elif kind == "curator":
    records = json.loads(prompt.split("CANDIDATE_SOURCES=", 1)[1])
    curator_calls = 0
    if log_path := os.environ.get("MODEL_LOG"):
        curator_calls = sum(
            json.loads(line).get("kind") == "curator"
            for line in Path(log_path).read_text().splitlines()
        )
    if os.environ.get("MODEL_INVALID_CURATOR") or (
        os.environ.get("MODEL_INVALID_CURATOR_ONCE") and curator_calls == 1
    ):
        print(json.dumps({"source_ids": ["unknown:source", records[0]["id"]]}))
    elif os.environ.get("MODEL_SELECT_ALL"):
        print(json.dumps({"source_ids": [record["id"] for record in records]}))
    else:
        print(json.dumps({"source_ids": [record["id"] for record in records[:2]]}))
elif kind == "grouper":
    records = json.loads(prompt.split("SELECTED_SOURCE_CARDS=", 1)[1])
    grouper_calls = 0
    if log_path := os.environ.get("MODEL_LOG"):
        with Path(log_path).open(encoding="utf-8") as log:
            grouper_calls = sum(json.loads(line)["kind"] == "grouper" for line in log)
    if os.environ.get("MODEL_INVALID_GROUPER_ONCE") and grouper_calls == 1:
        stories = [[records[0]["id"]]]
    elif os.environ.get("MODEL_GROUP_FIRST_TWO") and len(records) > 1:
        stories = [[record["id"] for record in records[:2]]]
        stories.extend([[record["id"]] for record in records[2:]])
    else:
        stories = [[record["id"]] for record in records]
    print(json.dumps({"stories": stories}))
elif kind == "writer":
    writer_calls = 0
    if log_path := os.environ.get("MODEL_LOG"):
        with Path(log_path).open(encoding="utf-8") as log:
            writer_calls = sum(json.loads(line)["kind"] == "writer" for line in log)
    if os.environ.get("MODEL_FAIL_WRITER") or (
        os.environ.get("MODEL_FAIL_WRITER_ONCE") and writer_calls == 1
    ):
        print("fixture writer failed", file=sys.stderr)
        raise SystemExit(9)
    stories = json.loads(prompt.split("SELECTED_STORIES=", 1)[1])
    if os.environ.get("MODEL_UNKNOWN_CITATION"):
        print("Unknown source [source:unknown:source]")
    else:
        lines = []
        for story in stories:
            lines.append(f"## {' + '.join(record['title'] for record in story)}")
            citations = " ".join(
                f"[source:{record['id']}]({record['url']})"
                if os.environ.get("MODEL_LINK_WRITER_ONCE") and writer_calls == 1
                else f"[source:{record['id']}]"
                for record in story
            )
            lines.append(f"Useful because it is concrete. {citations}")
            lines.append("")
        print("\n".join(lines))
