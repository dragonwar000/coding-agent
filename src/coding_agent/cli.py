"""`python -m coding_agent.cli`: the agent-facing commands for memory and reports.

- `recall <query> [-k N] [--exclude-session ID]`: search the project's Zero-Mem store.
- `stats`: how many turns and sessions the store holds.
- `report`: per guard, how many decisions were logged and how many the guard applied (FR-012).
- `forget <session-id>`: delete one session's records; the session id is required, never guessed.
- `brief --plan PLAN.md --task N`: the Global constraints and one task's Files and Interfaces (SC-005).
- `orca-create --t-id T-x --title ... --spec ...`: create an Orca task that carries the repo as `project` (FR-006).
- `orca-status --task-id ID --status S [--basis B] [--artifact PATH ...]`: apply a status under the completion rule (FR-007).
- `claim --task-id ID [--project NAME]`: refuse a task owned by another repository and log the refusal (FR-008).
- `orca-check --tasks FILE [--strict]`: report `CLAIMED-DONE BUT ABSENT`; `--strict` exits 1 when any is found (SC-002).
- `status`: this repository's Orca tasks by state, read from the live Orca CLI (FR-014, SC-001).
- `delegate --title ... --spec ... [--agent claude|codex] [--t-id T-x]`: create a task and start an Orca worker on it in its own worktree (coordinator).
- `board`: the task board the coordinator sees on each prompt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import os
import uuid

from coding_agent import coordinator, events, orca, orca_cli
from coding_agent.brief import BriefError, brief
from coding_agent.manifest import ManifestError, load
from coding_agent.memory import zeromem


def _root(path: str | None) -> Path:
    return Path(path).resolve() if path else Path.cwd().resolve()


def _store(root: Path):
    embedder = "hash"
    manifest_path = root / "integration.yaml"
    if manifest_path.exists():
        try:
            embedder = load(manifest_path).memory.embedder
        except ManifestError as error:
            raise SystemExit(f"cli: {error}") from error
    store = zeromem.store_for(root, embedder=embedder)
    zeromem.ensure_models(store)
    return store


def _status(root: Path, run: str | None) -> int:
    """Group this repository's Orca tasks by state. Tasks are attributed to a repository by the link ledger, not by Orca."""
    project = orca.project_name(root)
    owners = orca_cli.links(root)
    try:
        tasks = orca_cli.list_tasks(run)
    except orca_cli.OrcaError as error:
        print(f"status: {error}", file=sys.stderr)
        return 1
    mine = [{**task, "project": owners[str(task.get("id"))]} for task in tasks if owners.get(str(task.get("id"))) == project]
    groups = orca.reconcile(mine, project=project)
    view = {
        name: [{"id": t.get("id"), "title": t.get("task_title") or t.get("title"), "status": t.get("status"), "basis": orca.basis_of(t)} for t in items]
        for name, items in groups.items()
    }
    print(json.dumps({"project": project, "counts": {name: len(items) for name, items in view.items()}, "tasks": view}, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coding_agent.cli")
    parser.add_argument("--root", help="project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    recall = sub.add_parser("recall", help="search the project's memory")
    recall.add_argument("query")
    recall.add_argument("-k", type=int, default=5)
    recall.add_argument("--exclude-session")

    sub.add_parser("stats", help="turns and sessions in the project's memory")
    sub.add_parser("report", help="guard decisions from the event log")
    forget = sub.add_parser("forget", help="delete one session from the project's memory")
    forget.add_argument("session_id")

    plan = sub.add_parser("brief", help="the brief for one PLAN task")
    plan.add_argument("--plan", required=True, type=Path)
    plan.add_argument("--task", required=True, type=int)

    create = sub.add_parser("orca-create", help="create an Orca task for this repository")
    create.add_argument("--t-id", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--spec", required=True)
    create.add_argument("--run", help="Orca Run id (default: CODING_AGENT_ORCA_RUN or the CLI's bound Run)")

    status = sub.add_parser("orca-status", help="set an Orca task's status under the completion rule")
    status.add_argument("--task-id", required=True)
    status.add_argument("--status", required=True)
    status.add_argument("--basis")
    status.add_argument("--artifact", action="append", default=[])
    status.add_argument("--run")

    claim = sub.add_parser("claim", help="claim an Orca task for this repository")
    claim.add_argument("--task-id", required=True)
    claim.add_argument("--project")

    listing = sub.add_parser("status", help="this repository's Orca tasks by state")
    listing.add_argument("--run")
    sub.add_parser("board", help="the task board the coordinator sees")

    delegate = sub.add_parser("delegate", help="create a task and start an Orca worker on it")
    delegate.add_argument("--title", required=True)
    delegate.add_argument("--spec", required=True)
    delegate.add_argument("--t-id", help="durable task id (default: a new one)")
    delegate.add_argument("--agent", help="Orca agent id (default: CODING_AGENT_WORKER_AGENT or claude)")
    delegate.add_argument("--run", help="Orca Run id (default: CODING_AGENT_ORCA_RUN or the bound Run)")

    check = sub.add_parser("orca-check", help="find tasks that claim completion while an artifact is missing")
    check.add_argument("--tasks", required=True, type=Path)
    check.add_argument("--strict", action="store_true")

    args = parser.parse_args(argv)
    root = _root(args.root)

    try:
        if args.command == "report":
            print(json.dumps(events.summarize(root), indent=2, ensure_ascii=False))
            return 0
        if args.command == "status":
            return _status(root, args.run)
        if args.command == "board":
            lines, error = coordinator.read_board(root)
            if error is not None:
                print(f"board: {error}", file=sys.stderr)
                return 1
            print("\n".join(lines))
            return 0
        if args.command == "delegate":
            task_id, dispatch = orca_cli.delegate(
                root,
                title=args.title,
                spec=args.spec,
                t_id=args.t_id or f"T-{uuid.uuid4().hex[:8]}",
                agent=args.agent or os.environ.get("CODING_AGENT_WORKER_AGENT", "claude"),
                run=args.run,
            )
            print(json.dumps({"task_id": task_id, "dispatch": dispatch}, ensure_ascii=False))
            return 0
        if args.command == "brief":
            print(brief(args.plan.read_text(encoding="utf-8"), args.task), end="")
            return 0
        if args.command == "orca-create":
            task_id = orca_cli.create_task(root, project=orca.project_name(root), t_id=args.t_id, spec=args.spec, title=args.title, run=args.run)
            print(json.dumps({"t_id": args.t_id, "task_id": task_id, "project": orca.project_name(root)}, ensure_ascii=False))
            return 0
        if args.command == "orca-status":
            outcome = orca_cli.set_status(root, task_id=args.task_id, requested=args.status, basis=args.basis, artifacts=tuple(args.artifact), run=args.run)
            print(json.dumps({"decision": outcome.decision, "sent": outcome.sent, "reason": outcome.reason}, ensure_ascii=False))
            return 0
        if args.command == "claim":
            try:
                orca_cli.claim(root, task_id=args.task_id, current_project=args.project or orca.project_name(root))
            except orca.ClaimRefused as error:
                print(f"claim: refused: {error}", file=sys.stderr)
                return 1
            print("claim: allowed")
            return 0
        if args.command == "orca-check":
            tasks = orca_cli.tasks_of(json.loads(args.tasks.read_text(encoding="utf-8")))
            findings = [f for task in tasks for f in orca.check_artifacts(task, root)]
            for finding in findings:
                print(f"{finding.code}: {finding.task} ({finding.detail})")
            print(f"orca-check: {len(findings)} finding(s) in {len(tasks)} task(s)")
            return 1 if findings and args.strict else 0
        store = _store(root)
        if args.command == "recall":
            print(json.dumps(zeromem.recall(store, args.query, top_k=args.k, exclude_session=args.exclude_session), indent=2, ensure_ascii=False))
        elif args.command == "stats":
            print(json.dumps(zeromem.stats(store), indent=2, ensure_ascii=False))
        elif args.command == "forget":
            print(json.dumps(zeromem.forget_session(store, args.session_id), indent=2, ensure_ascii=False))
    except zeromem.ZeromemError as error:
        print(f"cli: {error}", file=sys.stderr)
        return 1
    except (BriefError, orca_cli.OrcaError, OSError, json.JSONDecodeError) as error:
        print(f"cli: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
