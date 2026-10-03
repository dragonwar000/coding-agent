"""Per-session state in `<root>/.coding-agent/state/<session>.json` (FR-001, FR-022).

Writes are atomic: a crash leaves the previous file, never a truncated one. Session ids are
reduced to letters, digits, `-`, and `_` before they become a file name.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

PROMPT_TTL_S = 7 * 24 * 3600
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _path(root: Path, session: str) -> Path:
    name = _SAFE.sub("-", session) or "default"
    return root / ".coding-agent" / "state" / f"{name[:120]}.json"


def load(root: Path, session: str) -> dict[str, Any]:
    path = _path(root, session)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(root: Path, session: str, data: dict[str, Any]) -> None:
    path = _path(root, session)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def expire_prompts(data: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Drop stored prompts older than seven days, the default retention chosen in the proposal."""
    cutoff = (now if now is not None else time.time()) - PROMPT_TTL_S
    prompts = data.get("prompts") or []
    data["prompts"] = [p for p in prompts if isinstance(p, dict) and p.get("ts", 0) >= cutoff]
    return data
