from __future__ import annotations

import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
kind = "curator" if "CANDIDATE_SOURCES=" in prompt else "writer"

if log_path := os.environ.get("MODEL_LOG"):
    with Path(log_path).open("a", encoding="utf-8") as log:
        log.write(json.dumps({"kind": kind, "prompt": prompt}) + "\n")

if kind == "curator":
    records = json.loads(prompt.split("CANDIDATE_SOURCES=", 1)[1])
    if os.environ.get("MODEL_INVALID_CURATOR"):
        print(json.dumps({"source_ids": ["unknown:source", records[0]["id"]]}))
    elif os.environ.get("MODEL_SELECT_ALL"):
        print(json.dumps({"source_ids": [record["id"] for record in records]}))
    else:
        print(json.dumps({"source_ids": [record["id"] for record in records[:2]]}))
else:
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
