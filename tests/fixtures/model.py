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
elif "CANDIDATE_SOURCES=" in prompt:
    kind = "curator"
elif "Edit draft.md in place" in prompt:
    kind = "editor"
else:
    kind = "writer"

event: dict[str, object] = {"kind": kind, "prompt": prompt}
if kind == "editor":
    event["draft"] = Path("draft.md").read_text()
    event["sources"] = json.loads(Path("sources.json").read_text())
if log_path := os.environ.get("MODEL_LOG"):
    with Path(log_path).open("a", encoding="utf-8") as log:
        log.write(json.dumps(event) + "\n")

if kind == "reader-cards":
    records = json.loads(prompt.split("SOURCE_RECORDS=", 1)[1])
    if os.environ.get("MODEL_READER_DROP_CARD") and len(records) > 1:
        records = records[:-1]
    if os.environ.get("MODEL_READER_UNKNOWN_CARD") and len(records) > 1:
        records[-1]["id"] = "rss-arxiv:204795ac0a186819eb0b270d"
    print(
        json.dumps(
            {
                "cards": [
                    {
                        "id": record["id"],
                        "card": (
                            f"{record['title']} asks a concrete question and reports useful "
                            "evidence that can be learned from."
                        ),
                    }
                    for record in records
                ]
            }
        )
    )
elif kind == "reader-check":
    records_json, sections_json = prompt.split("SOURCE_RECORDS=", 1)[1].split(
        "\n\nBRIEFING_SECTIONS=", 1
    )
    records = json.loads(records_json)
    sections = json.loads(sections_json)
    source_ids = []
    for section in sections:
        matches = [record["id"] for record in records if record["title"] == section["heading"]]
        source_ids.append(matches)
    if os.environ.get("MODEL_CHECKER_SWAP") and len(source_ids) > 1:
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
elif kind == "writer":
    if os.environ.get("MODEL_FAIL_WRITER"):
        print("fixture writer failed", file=sys.stderr)
        raise SystemExit(9)
    records = json.loads(prompt.split("SELECTED_SOURCES=", 1)[1])
    if os.environ.get("MODEL_UNKNOWN_CITATION"):
        print("Unknown source [source:unknown:source]")
    else:
        lines = ["# Daily Briefing", ""]
        for record in records:
            lines.append(f"## {record['title']}")
            lines.append(f"Useful because it is concrete. [source:{record['id']}]")
            lines.append("")
        print("\n".join(lines))
else:
    if os.environ.get("MODEL_FAIL_EDITOR"):
        print("fixture editor failed", file=sys.stderr)
        raise SystemExit(10)
    if os.environ.get("MODEL_EDITOR_NO_COMPLETE"):
        print("I could not edit the draft")
        raise SystemExit
    draft = Path("draft.md").read_text()
    if os.environ.get("MODEL_EDITOR_UNKNOWN_CITATION"):
        draft += "\nUnknown source [source:unknown:source]\n"
    if os.environ.get("MODEL_EDITOR_DROP_CITATION"):
        source_id = json.loads(Path("sources.json").read_text())[0]["id"]
        draft = draft.replace(f"[source:{source_id}]", "", 1)
    Path("draft.md").write_text(draft.replace("Useful because", "Clear because"))
    print("EDIT_COMPLETE")
