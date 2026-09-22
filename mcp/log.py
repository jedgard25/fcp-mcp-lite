"""JSONL call log. Every MCP tool call appends one line: tool, args, bridge
RPCs, result summary, timing. tail -f it via `make logs`."""

import datetime
import json
import os

LOG_DIR = os.path.expanduser("~/.local/share/fcp-mcp-lite")
LOG_PATH = os.path.join(LOG_DIR, "calls.jsonl")


def record(tool: str, args: dict, rpcs: list, result: dict, ms: int,
           mcp_version: str | None = None) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tool": tool,
        "args": args,
        "rpcs": rpcs,
        "result": result,
        "ms": ms,
    }
    if mcp_version is not None:
        entry["mcp_version"] = mcp_version
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")
