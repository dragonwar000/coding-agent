#!/usr/bin/env python3
"""Fake `orca` CLI for tests. Tasks live in `$FAKE_ORCA_STATE` as JSON; answers follow the envelope the adapter expects.

Like the real CLI, an unknown id or a status outside the enum answers `ok: false` with exit 0, and the
task commands without `--run` (and no bound run) answer `run_required` with exit 1.
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
    if argv[:2] == ["worktree", "create"]:
        options = _options(argv[2:])
        path = os.path.join(os.path.dirname(str(state_path)), "worktrees", options["--name"])
        data.setdefault("created_worktrees", []).append({"name": options["--name"], "repo": options.get("--repo")})
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"ok": True, "result": {"worktree": {"id": f"repo::{path}", "path": path}}}))
        return 0
    if argv[:1] != ["orchestration"] or len(argv) < 2:
        print(json.dumps({"ok": False, "error": "unknown command"}))
        return 0
    command, options = argv[1], _options(argv[2:])
    if command in ("task-create", "task-update", "task-list", "worker-start") and "--run" not in options:
        print(json.dumps({"id": str(uuid.uuid4()), "ok": False, "error": {"code": "run_required", "message": "No Run is bound."}}))
        return 1

    if command == "check":
        messages = data.setdefault("messages", [])
        if "--ack" in options:
            data["messages"] = [m for m in messages if m.get("delivery") != options["--ack"]]
            answer = {"ok": True, "result": {"runId": options.get("--run"), "messages": [], "count": 0}}
        elif "--peek" in argv:
            answer = {"ok": True, "result": {"runId": options.get("--run"), "messages": messages, "count": len(messages)}}
        else:
            for m in messages:
                m["delivery"] = "delivery_1"
            answer = {"ok": True, "result": {"runId": options.get("--run"), "messages": messages, "count": len(messages), "deliveryId": "delivery_1"}}
    elif command == "run-create":
        answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"run": {"id": "run_created", "objective": options.get("--objective", "")}}}
    elif command == "task-create":
        task_id = f"task_{len(tasks) + 1:04d}"
        tasks[task_id] = {"id": task_id, "title": options.get("--task-title", ""), "spec": options.get("--spec", ""), "status": "pending" if json.loads(options.get("--deps", "[]")) else "ready", "result": None,
                          "deps": options.get("--deps", "[]")}
        answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"task": {"id": task_id, "status": tasks[task_id]["status"]}}}
    elif command == "task-update":
        task = tasks.get(options.get("--id", ""))
        status = options.get("--status", "")
        if task is None or status not in STATUSES:
            answer = {"ok": False, "error": "unknown task or status"}
        else:
            task["status"] = status
            task["result"] = options.get("--result")
            if status == "completed":
                for other in tasks.values():
                    if other["status"] == "pending" and all(tasks[d]["status"] == "completed" for d in json.loads(other["deps"])):
                        other["status"] = "ready"
            answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"task": {"id": task["id"], "status": status}}}
    elif command == "worker-start":
        task = tasks.get(options.get("--task", ""))
        if options.get("--worktree") == "new-child" and os.environ.get("FAKE_ORCA_NO_NEW_CHILD") == "1":
            answer = {"ok": False, "error": {"code": "selector_not_found", "message": "selector_not_found"}}
        elif options.get("--worktree") == "new-child" and "--name" not in options:
            answer = {"ok": False, "error": {"code": "invalid_argument", "message": "New worktrees require --name."}}
        elif task is None or options.get("--agent") is None:
            answer = {"ok": False, "error": {"code": "unknown_task", "message": "no such task or agent"}}
        else:
            dispatch_id = f"dsp_{len(data.setdefault('dispatches', {})) + 1:04d}"
            selector = options.get("--worktree", "")
            worktree = selector[5:] if selector.startswith("path:") else os.path.join(os.path.dirname(str(state_path)), "worktrees", options["--name"])
            data["dispatches"][dispatch_id] = {"task": task["id"], "agent": options["--agent"], "worktree": selector, "name": options.get("--name", os.path.basename(worktree)),
                                               "display": options.get("--display-name")}
            task["status"] = "dispatched"
            answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"runId": options["--run"], "taskId": task["id"], "dispatchId": dispatch_id,
                                                                 "effects": [{"kind": "worktree", "action": "created_child", "id": f"repo::{worktree}"}]}}
    elif command == "task-list":
        answer = {"ok": True, "result": {"tasks": list(tasks.values())}}
    else:
        answer = {"ok": False, "error": f"unknown subcommand {command}"}

    state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
