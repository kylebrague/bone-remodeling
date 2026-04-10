#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

MUTATING_SHELL_PATTERNS = (
    re.compile(r"\bgit\s+(add|commit|push|switch\b|checkout\b|merge\b|rebase\b)"),
    re.compile(r"\b(rm|mv|cp|mkdir|touch)\b"),
    re.compile(r">\s*\S"),
    re.compile(r"\btee\b"),
    re.compile(r"\bsed\s+-i\b"),
)

WRITE_TOOL_HINTS = ("write", "edit", "patch")


def _deny(reason: str) -> int:
    print(
        json.dumps(
            {
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        )
    )
    return 0


def _load_input() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)


def _is_write_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return any(hint in lowered for hint in WRITE_TOOL_HINTS)


def _is_mutating_shell(command: str) -> bool:
    return any(pattern.search(command) for pattern in MUTATING_SHELL_PATTERNS)


def main() -> int:
    payload = _load_input()
    tool_name = str(payload.get("toolName", ""))
    tool_args_raw = payload.get("toolArgs", "")

    read_only = os.environ.get("OSTEOBLAST_READ_ONLY", "0") == "1"
    serious_mode = os.environ.get("OSTEOBLAST_FINDING_SEVERITY", "") == "serious"

    if not read_only and not serious_mode:
        return 0

    if _is_write_tool(tool_name):
        return _deny("Osteoblast policy blocked a write-capable tool in read-only mode.")

    if tool_name.lower() != "bash":
        return 0

    if not isinstance(tool_args_raw, str):
        return 0

    try:
        tool_args = json.loads(tool_args_raw)
    except json.JSONDecodeError:
        return 0

    command = str(tool_args.get("command", ""))
    if command and _is_mutating_shell(command):
        return _deny("Osteoblast policy blocked a mutating shell command in read-only mode.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
