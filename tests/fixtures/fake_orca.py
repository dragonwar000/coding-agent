#!/usr/bin/env python3
"""Fake `orca` CLI for tests. Tasks live in `$FAKE_ORCA_STATE` as JSON; answers follow the envelope the adapter expects.

Like the real CLI, an unknown id or a status outside the enum answers `ok: false` with exit 0.
`FAKE_ORCA_FAIL=1` exits 3 with a message on stderr, to test the adapter's failure path.
"""

import json
import os
import sys
import uuid
from pathlib import Path

STATUSES = {"pending", "ready", "dispatched", "completed", "failed", "blocked"}


def _options(argv: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for index, token in enumerate(argv):
        if token.startswith("--") and index + 1 < len(argv) and not argv[index + 1].startswith("--"):
            found[token] = argv[index + 1]
    return found


def main(argv: list[str]) -> int:
    if os.environ.get("FAKE_ORCA_FAIL") == "1":
        print("orca: daemon not running", file=sys.stderr)
        return 3
    state_path = Path(os.environ["FAKE_ORCA_STATE"])
    data = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"tasks": {}}
    tasks = data["tasks"]
    if argv[:1] != ["orchestration"] or len(argv) < 2:
        print(json.dumps({"ok": False, "error": "unknown command"}))
        return 0
    command, options = argv[1], _options(argv[2:])

    if command == "task-create":
        task_id = f"task_{len(tasks) + 1:04d}"
        tasks[task_id] = {"id": task_id, "title": options.get("--task-title", ""), "spec": options.get("--spec", ""), "status": "pending", "result": None}
        answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"task": {"id": task_id, "status": "pending"}}}
    elif command == "task-update":
        task = tasks.get(options.get("--id", ""))
        status = options.get("--status", "")
        if task is None or status not in STATUSES:
            answer = {"ok": False, "error": "unknown task or status"}
        else:
            task["status"] = status
            task["result"] = json.loads(options["--result"]) if "--result" in options else None
            answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"task": {"id": task["id"], "status": status}}}
    elif command == "task-list":
        answer = {"ok": True, "result": {"tasks": list(tasks.values())}}
    else:
        answer = {"ok": False, "error": f"unknown subcommand {command}"}

    state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
