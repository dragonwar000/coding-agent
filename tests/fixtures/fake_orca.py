#!/usr/bin/env python3
"""Fake `orca` CLI for tests. Tasks live in `$FAKE_ORCA_STATE` as JSON; answers follow the envelope the adapter expects.

Like the real CLI, an unknown id or a status outside the enum answers `ok: false` with exit 0, and the
task commands without `--run` (and no bound run) answer `run_required` with exit 1.
`FAKE_ORCA_FAIL=1` exits 3 with a message on stderr, to test the adapter's failure path.
`FAKE_ORCA_EXIT=<n>` makes every answer exit `n` after printing it, like the real CLI's `ok: true` with exit 1.
`FAKE_ORCA_TURN_UNOBSERVED=1` makes `worker-start` answer the measured "turn start unobserved" shape (no `effects`,
exit 1) and `terminal send` exit 1 with `ok: true`. `FAKE_ORCA_DEAF=1` makes the fake terminal's screen never show
what was sent to it. `worktree create --agent` answers an `agentTerminalHandle` unless `FAKE_ORCA_NO_AGENT_TERMINAL=1`,
and `worker-start` takes `--terminal` in place of `--agent`. `terminal read` shows a prompt line plus the texts sent so far; `dispatch-show`, `worker-show`,
and `worker-abandon` read and change the dispatch records that `worker-start` creates.
`FAKE_ORCA_FOLDER_PROJECT=1` makes `worker-start --worktree new-child` answer the refusal Orca gives a terminal of a
folder project (the harness no longer asks for `new-child`). `FAKE_ORCA_UNKNOWN_REPO=1` makes `worktree create` answer `repo_not_found` until `repo add` registered
the repository. `FAKE_ORCA_REAL_WORKTREE=1` makes `worktree create` add a real git worktree of the named repository.
`terminal create` records `--worktree`, `--title`, and `--command` and answers `result.terminal.handle`.
`FAKE_ORCA_SCREEN` replaces what `terminal read` shows (lines separated by `\\n`). With `$CLAUDE_CONFIG_DIR` set,
`worktree create` and `terminal create` copy `.claude.json` as it is at that moment to `$FAKE_ORCA_STATE.<word>-create.json`,
so a test can see what was trusted when each call came.
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
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", "")) / ".claude.json"
    if argv[1:2] == ["create"] and os.environ.get("CLAUDE_CONFIG_DIR") and config.exists():
        Path(f"{state_path}.{argv[0]}-create.json").write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    tasks = data["tasks"]
    if argv[:2] == ["worktree", "create"]:
        options = _options(argv[2:])
        path = os.path.join(os.path.dirname(str(state_path)), "worktrees", options["--name"])
        repo = (options.get("--repo") or "")[5:]
        if os.environ.get("FAKE_ORCA_UNKNOWN_REPO") == "1" and repo not in data.get("repos", []):
            data.setdefault("refused_creates", []).append(repo)
            state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"ok": False, "error": {"code": "repo_not_found", "message": f"No repo matches path:{repo}"}}))
            return 1
        data.setdefault("created_worktrees", []).append({"name": options["--name"], "repo": options.get("--repo"), "agent": options.get("--agent")})
        data.setdefault("create_calls", []).append({"base": options.get("--base-branch"), "no_parent": "--no-parent" in argv})
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if os.environ.get("FAKE_ORCA_REAL_WORKTREE") == "1":
            import subprocess
            subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "-b", options["--name"], path, *([options["--base-branch"]] if "--base-branch" in options else [])],
                           check=True, capture_output=True)
        result = {"worktree": {"id": f"repo::{path}", "path": path}}
        if "--agent" in options and os.environ.get("FAKE_ORCA_NO_AGENT_TERMINAL") != "1":
            result["agentTerminalHandle"] = f"term_wt_{options['--name']}"
        print(json.dumps({"ok": True, "result": result}))
        return 0
    if argv[:2] == ["repo", "add"]:
        data.setdefault("repos", []).append(_options(argv[2:])["--path"])
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"ok": True, "result": {"repo": {"path": data["repos"][-1]}}}))
        return 0
    if argv[:2] == ["worktree", "rm"]:
        options = _options(argv[2:])
        path = options.get("--worktree", "")[5:]
        if os.environ.get("FAKE_ORCA_RM_FAIL") == "1":
            print(json.dumps({"ok": False, "error": {"code": "worktree_dirty", "message": "worktree has changes"}}))
            return 1
        data.setdefault("removed_worktrees", []).append({"path": path, "force": "--force" in argv})
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if os.path.isdir(path):
            import subprocess
            # Run the removal from the main worktree: on Windows, git cannot delete the directory it was started in.
            common = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True).stdout.strip()
            main_tree = os.path.dirname(common) if common else path
            subprocess.run(["git", "-C", main_tree, "worktree", "remove", "--force", path], capture_output=True)
        print(json.dumps({"ok": True, "result": {"removed": True}}))
        return 0
    exit_code = int(os.environ.get("FAKE_ORCA_EXIT") or 0)
    if argv[:2] == ["terminal", "read"]:
        options = _options(argv[2:])
        handle = options.get("--terminal", "")
        sent = [] if os.environ.get("FAKE_ORCA_DEAF") == "1" else [m["text"] for m in data.get("sent", []) if m["terminal"] == handle]
        tail = ["❯ "] + [f"❯ {text}" for text in sent]
        if "FAKE_ORCA_SCREEN" in os.environ:
            tail = os.environ["FAKE_ORCA_SCREEN"].split("\n")
        print(json.dumps({"ok": True, "result": {"terminal": {"handle": handle, "status": "running", "tail": tail, "source": "screen"}}}, ensure_ascii=False))
        return exit_code
    if argv[:2] == ["terminal", "create"]:
        options = _options(argv[2:])
        created = data.setdefault("created_terminals", [])
        handle = f"term_created_{len(created) + 1}"
        created.append({"handle": handle, "worktree": options.get("--worktree"), "title": options.get("--title"), "command": options.get("--command")})
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"ok": True, "result": {"terminal": {"handle": handle}}}))
        return exit_code
    if argv[:2] == ["terminal", "send"]:
        options = _options(argv[2:])
        data.setdefault("sent", []).append({"terminal": options.get("--terminal"), "text": options.get("--text", ""), "enter": "--enter" in argv,
                                            "wait_submit": options.get("--wait-submit")})
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        unobserved = os.environ.get("FAKE_ORCA_TURN_UNOBSERVED") == "1"
        print(json.dumps({"ok": True, "result": {"accepted": True, "submission": "unobserved" if unobserved else "observed"}}))
        return 1 if unobserved else exit_code
    if argv[:1] != ["orchestration"] or len(argv) < 2:
        print(json.dumps({"ok": False, "error": "unknown command"}))
        return 0
    command, options = argv[1], _options(argv[2:])
    if command in ("dispatch-show", "worker-show", "worker-abandon"):
        dispatches = data.setdefault("dispatches", {})
        if command == "dispatch-show":
            found = [(d_id, d) for d_id, d in dispatches.items() if d["task"] == options.get("--task")]
            if not found:
                answer = {"ok": False, "error": {"code": "dispatch_not_found", "message": f"no dispatch for task {options.get('--task')}"}}
            else:
                d_id, d = found[-1]
                answer = {"ok": True, "result": {"dispatch": {"id": d_id, "task_id": d["task"], "assignee_handle": f"term_{d['task']}", "status": d.get("status", "pending")}}}
        elif command == "worker-show":
            d = dispatches.get(options.get("--dispatch", ""))
            if d is None:
                answer = {"ok": False, "error": {"code": "dispatch_not_found", "message": "no such dispatch"}}
            else:
                answer = {"ok": True, "result": {"dispatch": {"id": options["--dispatch"], "status": d.get("status", "pending")},
                                                 "worker": {"dispatchId": options["--dispatch"], "state": d.get("state", "start_unknown")}}}
        else:
            d = dispatches.get(options.get("--dispatch", ""))
            if d is None:
                answer = {"ok": False, "error": {"code": "dispatch_not_found", "message": "no such dispatch"}}
            else:
                d["state"], d["status"] = "abandoned", "abandoned"
                data.setdefault("abandoned", []).append(options["--dispatch"])
                answer = {"ok": True, "result": {"dispatchId": options["--dispatch"], "state": "abandoned"}}
        state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(answer, ensure_ascii=False))
        return exit_code
    if command in ("task-create", "task-update", "task-list", "worker-start") and "--run" not in options:
        print(json.dumps({"id": str(uuid.uuid4()), "ok": False, "error": {"code": "run_required", "message": "No Run is bound."}}))
        return 1

    if command == "worker-release":
        data.setdefault("released", []).append(options.get("--dispatch"))
        answer = {"ok": True, "result": {"dispatchId": options.get("--dispatch"), "state": "released"}}
    elif command == "check":
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
        elif options.get("--worktree") == "new-child" and os.environ.get("FAKE_ORCA_FOLDER_PROJECT") == "1":
            answer = {"ok": False, "error": {"code": "invalid_argument", "message": "Folder projects cannot create orchestration worktrees; use current or an exact existing folder workspace."}}
        elif options.get("--worktree") == "new-child" and "--name" not in options:
            answer = {"ok": False, "error": {"code": "invalid_argument", "message": "New worktrees require --name."}}
        elif task is None or (options.get("--agent") is None and options.get("--terminal") is None):
            answer = {"ok": False, "error": {"code": "unknown_task", "message": "no such task or agent"}}
        else:
            dispatch_id = f"dsp_{len(data.setdefault('dispatches', {})) + 1:04d}"
            selector = options.get("--worktree", "")
            worktree = selector[5:] if selector.startswith("path:") else os.path.join(os.path.dirname(str(state_path)), "worktrees", options["--name"])
            # A dispatch to an agent terminal runs the agent its worktree was created with.
            agent = (options.get("--agent") or next((w["agent"] for w in data.get("created_worktrees", []) if f"term_wt_{w['name']}" == options.get("--terminal")), None)
                     or next((t["command"].split()[0] for t in data.get("created_terminals", []) if t["handle"] == options.get("--terminal")), None))
            data["dispatches"][dispatch_id] = {"task": task["id"], "agent": agent, "terminal": options.get("--terminal"), "worktree": selector, "name": options.get("--name", os.path.basename(worktree)),
                                               "display": options.get("--display-name"), "base": options.get("--base-branch")}
            task["status"] = "dispatched"
            answer = {"ok": True, "id": str(uuid.uuid4()), "result": {"runId": options["--run"], "taskId": task["id"], "dispatchId": dispatch_id,
                                                                 "effects": [{"kind": "worktree", "action": "created_child", "id": f"repo::{worktree}"}]}}
            if os.environ.get("FAKE_ORCA_TURN_UNOBSERVED") == "1":
                del answer["result"]["effects"]
                answer["result"].update({"state": "outcome_unknown", "stage": "turn_start_unobserved", "turnStart": "unobserved",
                                         "lastError": "Dispatch input was written and submitted, but claude's turn start could not be verified"})
                exit_code = 1
    elif command == "task-list":
        answer = {"ok": True, "result": {"tasks": list(tasks.values())}}
    else:
        answer = {"ok": False, "error": f"unknown subcommand {command}"}

    state_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(answer, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
