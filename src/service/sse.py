from __future__ import annotations

import json


def format_buffered_sse(event_id: int, payload: str) -> str:
    lines = payload.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            data = json.loads(line[6:])
        except json.JSONDecodeError:
            break
        if isinstance(data, dict):
            data["event_id"] = event_id
            lines[index] = "data: " + json.dumps(data, ensure_ascii=False)
        break
    body = "\n".join(lines).rstrip("\n")
    return f"id: {event_id}\n{body}\n\n"
