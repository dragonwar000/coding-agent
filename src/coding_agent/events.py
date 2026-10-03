"""Append-only decision log: `<root>/.coding-agent/events.jsonl` (FR-003, FR-012).

Every guard decision is one line with `applied`, so a shadow decision and an enforced one stay
distinguishable. Writing the log never blocks a hook: callers wrap failures.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

LOG_DIR = ".coding-agent"
LOG_FILE = "events.jsonl"


def log_path(root: Path) -> Path:
    return root / LOG_DIR / LOG_FILE


def record(root: Path, *, guard: str, kind: str, mode: str, applied: bool, session: str | None = None, detail: dict[str, Any] | None = None) -> None:
    """Append one decision. `kind` names what happened; `applied` is true only when the guard acted."""
    path = log_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": int(time.time()),
        "guard": guard,
        "kind": kind,
        "mode": mode,
        "applied": applied,
        "session": session,
        "detail": detail or {},
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def summarize(root: Path) -> dict[str, dict[str, int]]:
    """Per guard: how many decisions were recorded and how many the guard applied (FR-012)."""
    totals: dict[str, Counter[str]] = {}
    path = log_path(root)
    if not path.exists():
        return {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        counter = totals.setdefault(str(item.get("guard", "?")), Counter())
        counter["decisions"] += 1
        if item.get("applied") is True:
            counter["applied"] += 1
    return {guard: {"decisions": c["decisions"], "applied": c["applied"]} for guard, c in sorted(totals.items())}
