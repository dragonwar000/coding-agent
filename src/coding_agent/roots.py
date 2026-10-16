"""Find the repositories a coordinator delegated into that `orca_cli.all_roots` does not see yet.

`all_roots` is the one list the board, the inbox, cleanup and `roots-list` read: the coordinator's directory, the
directories in its tree that hold a link ledger (`orca_cli.tree_roots`, found again on every read), and `roots.json`
for repositories outside the tree, which `delegate --root` registers (`orca_cli.register_root`). A repository outside
the tree that was delegated into before `roots.json` existed, or whose entry was lost, is not seen. Two ways to
recover it, both writing `roots.json` only:

- `discover`: for each task of the Run that no known link ledger owns, ask Orca which worktree its worker ran in
  (`orchestration worker-list`: `resource.worktreeId` or `projection.workspace.id`, both `<repoId>::<path>`), take the
  main repository of that worktree (`git rev-parse --git-common-dir`, or the main worktree Orca lists for the repoId
  when the worktree is gone), and register it when its own `links.jsonl` holds the task. It runs only when a task has
  no owner; a task it could not place is retried after `RETRY_S`, so a board turn stays cheap. The board, `inbox`
  and `cleanup.candidates` run it first. Orca or git errors are never raised: they are logged as `guard=roots` events.
- `add`: register paths by hand (`roots-add`).

Measured on 2026-10-11 against the installed Orca: `worker-list --run R --limit 100` answers `result.workers[]` with
`taskId`, `resource.worktreeId` and `projection.workspace.id`, paged by `result.page.nextCursor`; `worktree list`
answers `result.worktrees[]` with `repoId`, `path` and `isMainWorktree`.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from coding_agent import events, orca_cli

CACHE_FILE = Path(".coding-agent") / "state" / "roots-discovery.json"
RETRY_S = 600
TIMEOUT_S = 10
MAX_PAGES = 10


def known_tasks(root: Path) -> set[str]:
    """Task ids a known repository owns: in the link ledger or the workers ledger of a root `all_roots` lists."""
    known: set[str] = set()
    for other in orca_cli.all_roots(root):
        known.update(orca_cli.links(other))
        known.update(record["task_id"] for record in orca_cli.worker_records(other))
    return known


def discover(root: Path, task_ids: Iterable[str], run: str | None = None, session: str = "cli") -> list[Path]:
    """Register the repositories that own tasks of `task_ids` no known ledger owns. Returns the newly registered ones."""
    known = known_tasks(root)
    unknown = [task for task in dict.fromkeys(str(t) for t in task_ids if t) if task not in known]
    if not unknown:
        return []
    cache = _load_cache(root)
    now = time.time()
    pending = [task for task in unknown if now - cache.get(task, 0) >= RETRY_S]
    if not pending:
        return []
    try:
        worktrees = task_worktrees(run)
    except orca_cli.OrcaError as error:
        _record(root, "discover-failed", session, {"error": str(error)[:300], "tasks": len(pending)})
        _save_cache(root, {**cache, **{task: now for task in pending}})
        return []
    added: list[Path] = []
    mains: dict[str, Path] | None = None
    for task in pending:
        repo = None
        for repo_id, path in worktrees.get(task, []):
            candidate = main_repo(Path(path))
            if candidate is None:
                if mains is None:
                    mains = _main_worktrees()
                candidate = mains.get(repo_id)
            if candidate is not None and task in orca_cli.links(candidate):
                repo = candidate
                break
        if repo is None:
            cache[task] = now
            continue
        cache.pop(task, None)
        if orca_cli.register_root(root, repo):
            added.append(repo)
            _record(root, "registered", session, {"root": str(repo), "task_id": task, "via": "orca-worktree"})
    _save_cache(root, cache)
    return added


def task_worktrees(run: str | None = None) -> dict[str, list[tuple[str, str]]]:
    """Task id to the `(repoId, worktree path)` its workers ran in, from `orchestration worker-list`."""
    out: dict[str, list[tuple[str, str]]] = {}
    cursor: str | None = None
    for _ in range(MAX_PAGES):
        args = ["orchestration", "worker-list", "--limit", "100", *orca_cli._run_args(run), *(["--cursor", cursor] if cursor else [])]
        answer = orca_cli._call_tool(*args, timeout=TIMEOUT_S)
        result = answer.get("result") if isinstance(answer, dict) else None
        if not isinstance(result, dict):
            break
        for worker in result.get("workers") or []:
            if not isinstance(worker, dict) or not isinstance(worker.get("taskId"), str):
                continue
            for ident in _worktree_ids(worker):
                repo_id, path = ident.split("::", 1)
                if (repo_id, path) not in out.setdefault(worker["taskId"], []):
                    out[worker["taskId"]].append((repo_id, path))
        page = result.get("page")
        cursor = page.get("nextCursor") if isinstance(page, dict) else None
        if not cursor:
            break
    return out


def _worktree_ids(worker: dict[str, Any]) -> list[str]:
    resource = worker.get("resource") if isinstance(worker.get("resource"), dict) else {}
    projection = worker.get("projection") if isinstance(worker.get("projection"), dict) else {}
    workspace = projection.get("workspace") if isinstance(projection.get("workspace"), dict) else {}
    ids = [resource.get("worktreeId"), workspace.get("id")]
    return [ident for ident in dict.fromkeys(ids) if isinstance(ident, str) and "::" in ident]


def main_repo(worktree: Path) -> Path | None:
    """The main worktree of the repository `worktree` belongs to, or None when it is gone or not in git."""
    if not worktree.is_dir():
        return None
    try:
        run = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    common = Path(run.stdout.strip()) if run.returncode == 0 and run.stdout.strip() else None
    return common.parent.resolve() if common is not None and common.name == ".git" else None


def _main_worktrees() -> dict[str, Path]:
    """repoId to the path of its main worktree, from `orca worktree list`; {} when Orca cannot say."""
    try:
        answer = orca_cli._call_tool("worktree", "list", timeout=TIMEOUT_S)
    except orca_cli.OrcaError:
        return {}
    result = answer.get("result") if isinstance(answer, dict) else None
    out: dict[str, Path] = {}
    for item in (result.get("worktrees") if isinstance(result, dict) else None) or []:
        if isinstance(item, dict) and item.get("isMainWorktree") and isinstance(item.get("repoId"), str) and isinstance(item.get("path"), str):
            path = Path(item["path"])
            if path.is_dir():
                out[item["repoId"]] = path.resolve()
    return out


def add(root: Path, paths: Iterable[Path], session: str = "cli") -> list[tuple[Path, str]]:
    """Register `paths` by hand: `(path, added|listed|tree|missing|own)` for each.

    `tree` is a directory in the coordinator's tree, which `orca_cli.tree_roots` reads once it holds a link ledger,
    so `roots.json` does not list it.
    """
    out: list[tuple[Path, str]] = []
    for path in paths:
        path = path.resolve()
        if not path.is_dir():
            out.append((path, "missing"))
        elif path == root.resolve():
            out.append((path, "own"))
        elif orca_cli.in_tree(root, path):
            out.append((path, "tree"))
        elif orca_cli.register_root(root, path):
            out.append((path, "added"))
            _record(root, "registered", session, {"root": str(path), "via": "roots-add"})
        else:
            out.append((path, "listed"))
    return out


def listing(root: Path) -> list[tuple[Path, str]]:
    """`all_roots` with where each one comes from: `own`, `tree` or `roots.json`."""
    tree = set(orca_cli.tree_roots(root))
    out: list[tuple[Path, str]] = []
    for index, path in enumerate(orca_cli.all_roots(root)):
        out.append((path, "own" if index == 0 else "tree" if path in tree else "roots.json"))
    return out


def _load_cache(root: Path) -> dict[str, float]:
    try:
        data = json.loads((root / CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))} if isinstance(data, dict) else {}


def _save_cache(root: Path, cache: dict[str, float]) -> None:
    try:
        path = root / CACHE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass


def _record(root: Path, kind: str, session: str, detail: dict[str, Any]) -> None:
    try:
        events.record(root, guard="roots", kind=kind, mode="enforce", applied=kind == "registered", session=session, detail=detail)
    except OSError:
        pass
