"""Read one turn from a Claude Code transcript (JSONL).

A turn runs from the last human prompt to the current end of the file. The reader returns the
prompt, the final assistant text, and the successful changes the turn made, each cited by the
1-based line of the event that proves it. Lines that do not parse are skipped, so a damaged
transcript yields a smaller turn, never an exception.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Turn:
    turn: int = 0
    prompt: str | None = None
    prompt_line: int | None = None
    prompt_ts: int | None = None
    response: str = ""
    response_line: int | None = None
    response_ts: int | None = None
    changes: dict[str, int] = field(default_factory=dict)
    # Tool calls in this turn whose result is an error: a user denial, a hook block, or a failed command.
    denials: int = 0

    def change_list(self) -> list[tuple[str, int]]:
        """Changed paths with the line of their latest successful change, in file order."""
        return sorted(self.changes.items(), key=lambda item: item[1])


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def _epoch(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def read_turn(path: Path, change_tools: tuple[str, ...]) -> Turn:
    """The current turn of one transcript file."""
    turn = Turn()
    pending: dict[str, tuple[str, str]] = {}
    tools = set(change_tools)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return turn

    for number, raw in enumerate(lines, start=1):
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        content = message.get("content", entry.get("content"))
        stamp = _epoch(entry.get("timestamp"))
        kind = entry.get("type") or message.get("role")

        if kind == "user":
            blocks = content if isinstance(content, list) else []
            results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                for block in results:
                    use_id = block.get("tool_use_id")
                    if use_id in pending and not block.get("is_error", False):
                        name, file_path = pending.pop(use_id)
                        if name in tools and file_path:
                            turn.changes[file_path] = number
                    elif use_id in pending:
                        pending.pop(use_id)
                        turn.denials += 1
                continue
            text = _text_of(content).strip()
            if text:
                turn = Turn(turn=turn.turn + 1, prompt=text, prompt_line=number, prompt_ts=stamp)
                pending = {}
            continue

        if kind == "assistant":
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                    file_path = tool_input.get("file_path") or tool_input.get("path")
                    pending[str(block.get("id"))] = (str(block.get("name", "")), str(file_path) if file_path else "")
            text = _text_of(content).strip()
            if text:
                turn.response = text
                turn.response_line = number
                turn.response_ts = stamp
    return turn
