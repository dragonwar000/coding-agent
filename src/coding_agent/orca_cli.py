"""Adapter to the Orca CLI: create, claim, and transition tasks through `orca orchestration` (FR-006 to FR-008).

Every call goes through `binary()`, which reads `CODING_AGENT_ORCA` and defaults to `orca`, so the
tests run the same code against a fake CLI. Rules come from `orca.py`; this module only performs
them and records the outcome.

Orca's task commands act inside a Run. Pass `run` explicitly or set `CODING_AGENT_ORCA_RUN`; without one the
CLI answers `run_required`. `orca orchestration run-list` lists the Runs.

Measured against the installed Orca CLI on 2026-10-04: `run-create` answers `result.run.id`; `task-create` answers
`result.task.id`; `task-list` answers `result.tasks`, where `deps` and `result` are JSON strings; a task without
dependencies starts `ready`, one with open dependencies starts `pending`, and Orca moves it to `ready` when they
complete; `worker-start --worktree new-child` requires `--name` and answers `result.dispatchId` plus an `effects`
entry `{kind: worktree, id: "<repo>::<path>"}`; Orca marks a task `completed` when its worker reports success
(`result.provenance == "worker_report"`). `task-update` is not measured. `ok: false` in an answer is an error.

Measured on Orca CLI 1.4.206 (Windows, 2026-10-07): `worker-start` can answer `ok: true` and still exit 1, with
`result.stage == "turn_start_unobserved"` and `result.turnStart == "unobserved"`, when the worktree and the terminal were
created but the agent's first turn was not seen. The answer may then carry no `effects`. Such an answer is returned, with
the exit code in `_exit`; only `ok: false` or no JSON is an error. The worker's terminal is then at an empty prompt, so
`kick_worker` sends it the kick-off text through `orca terminal send`. The preamble a worker reads with `dispatch-show`
carries no capability token, so Orca rejects its `worker_done`; `settle` abandons the dispatch and completes the task
once the coordinator has checked the result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import events, orca

BIN_ENV = "CODING_AGENT_ORCA"
RUN_ENV = "CODING_AGENT_ORCA_RUN"
TIMEOUT_S = 60
EXIT_KEY = "_exit"


class OrcaError(RuntimeError):
    """The Orca CLI failed, printed no JSON, or answered `ok: false`."""


@dataclass(frozen=True)
class Transition:
    """What happened to a status request: `sent` says whether Orca was called, `decision` is the status applied or `unverified`."""

    decision: str
    sent: bool
    reason: str


def binary() -> str:
    return os.environ.get(BIN_ENV, "orca")


def run_id(explicit: str | None) -> str | None:
    """The Run to act in: the explicit id, else the environment, else None (the CLI then uses its bound Run)."""
    return explicit or os.environ.get(RUN_ENV) or None


RUN_FILE = Path(".coding-agent") / "orca-run"


def use_stored_run(root: Path) -> None:
    """Make the Run stored by `run-init` the default for this process, unless the environment already names one."""
    path = root / RUN_FILE
    if os.environ.get(RUN_ENV) or not path.exists():
        return
    stored = path.read_text(encoding="utf-8").strip()
    if stored:
        os.environ[RUN_ENV] = stored


def run_init(root: Path, objective: str) -> str:
    """Create an Orca Run from this terminal and store its id for later commands and hooks. Returns the Run id.

    Assumption, unverified against a live Orca: run-create answers the id at `result.run.id`, `result.id`, or `result.runId`.
    """
    answer = _call("run-create", "--objective", objective)
    result = answer.get("result") if isinstance(answer, dict) else None
    run = None
    if isinstance(result, dict):
        nested = result.get("run")
        run = (nested.get("id") if isinstance(nested, dict) else None) or result.get("runId") or result.get("id")
    if not isinstance(run, str) or not run:
        raise OrcaError("the run-create answer has no run id")
    path = root / RUN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run + "\n", encoding="utf-8")
    return run


def _run_args(run: str | None) -> list[str]:
    chosen = run_id(run)
    return ["--run", chosen] if chosen else []


def _call(*args: str) -> Any:
    """Run `orca orchestration <args> --json` and return the parsed answer."""
    return _run_orca("orchestration", *args)


def _call_tool(*args: str) -> Any:
    """Run `orca <args> --json`, for commands outside `orchestration`, and return the parsed answer."""
    return _run_orca(*args)


def _command(*words: str) -> list[str]:
    """The argv for `orca <words> --json`. A `.py` binary (the test fake) runs under this interpreter, which Windows needs."""
    exe = binary()
    prefix = [sys.executable, exe] if exe.lower().endswith(".py") else [exe]
    return [*prefix, *words, "--json"]


def _run_orca(*words: str) -> Any:
    """Run one Orca command and return its parsed answer.

    An `ok: true` answer is returned even when the process exits non-zero: Orca uses the exit code for an
    unverified outcome (a worker whose turn start was not observed), not for failure. The exit code is kept in
    `answer["_exit"]` for callers that care. `ok: false`, no JSON, or a failure to run the binary raise `OrcaError`.
    """
    args = words[1:] if words[0] == "orchestration" else words
    try:
        run = subprocess.run(_command(*words), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise OrcaError(f"cannot run {binary()}: {error}") from error
    try:
        answer = json.loads(run.stdout)
    except json.JSONDecodeError as error:
        if run.returncode != 0:
            raise OrcaError(f"orca {args[0]} exited {run.returncode}: {(run.stderr or run.stdout).strip()[:400]}") from error
        raise OrcaError(f"orca {args[0]} printed no JSON") from error
    if isinstance(answer, dict) and answer.get("ok") is False:
        raise OrcaError(f"orca {args[0]} answered ok:false: {_error_message(answer)}")
    if run.returncode != 0:
        if isinstance(answer, dict) and answer.get("ok") is True:
            answer[EXIT_KEY] = run.returncode
            return answer
        raise OrcaError(f"orca {args[0]} exited {run.returncode}: {(run.stderr or run.stdout).strip()[:400]}")
    return answer


def _error_message(answer: dict[str, Any]) -> str:
    """The short message from an `ok: false` answer, such as `run_required`."""
    error = answer.get("error")
    if isinstance(error, dict):
        return f"{error.get('code', 'error')}: {error.get('message', '')}"[:400]
    return json.dumps(answer, ensure_ascii=False)[:400]


def task_id_of(answer: Any) -> str:
    """The `task_xxx` id from a task-create or task-update answer. The envelope `id` is a different uuid."""
    try:
        return str(answer["result"]["task"]["id"])
    except (KeyError, TypeError) as error:
        raise OrcaError("the answer has no result.task.id") from error


def tasks_of(answer: Any) -> list[dict[str, Any]]:
    """The task records in a task-list answer, or in an export file with the same shape."""
    body = answer.get("result", answer) if isinstance(answer, dict) else answer
    tasks = body.get("tasks", body) if isinstance(body, dict) else body
    if not isinstance(tasks, list):
        raise OrcaError("the answer has no task list")
    return [_decoded(task) for task in tasks if isinstance(task, dict)]


def _decoded(task: dict[str, Any]) -> dict[str, Any]:
    """A task record with `deps` and `result` parsed: Orca stores both as JSON strings."""
    out = dict(task)
    for key in ("deps", "result"):
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return out


def links(root: Path) -> dict[str, str]:
    """Orca task id to the repository that created it, from the link ledger. The last link wins."""
    path = root / ".coding-agent" / "links.jsonl"
    if not path.exists():
        return {}
    owners: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("task_id"), str) and isinstance(record.get("project"), str):
            owners[record["task_id"]] = record["project"]
    return owners


def owner_of(root: Path, task_id: str) -> str | None:
    """The repository that created `task_id`, or None when the ledger does not know it."""
    return links(root).get(task_id)


def create_task(root: Path, *, project: str, t_id: str, spec: str, title: str, run: str | None = None, deps: tuple[str, ...] = ()) -> str:
    """Create an Orca task that carries the durable `T-id` and the `project`, then link the two ids (FR-006).

    `deps` are the Orca task ids this task depends on; Orca records them with the task.
    """
    header = f"project: {project}\nt_id: {t_id}\n\n"
    dep_args = ["--deps", json.dumps(list(deps))] if deps else []
    task_id = task_id_of(_call("task-create", "--spec", header + spec, "--task-title", title, *dep_args, *_run_args(run)))
    orca.link(root, t_id=t_id, task_id=task_id, project=project)
    return task_id


def claim(root: Path, *, task_id: str, current_project: str, session: str = "cli") -> None:
    """Refuse a claim from another repository and record the refusal (FR-008). Returns when the claim is allowed."""
    owner = owner_of(root, task_id)
    try:
        orca.claim_allowed({"id": task_id, "project": owner}, current_project)
    except orca.ClaimRefused as error:
        events.record(root, guard="orca-project", kind="claim-refused", mode="enforce", applied=True, session=session,
                      detail={"task_id": task_id, "owner": owner, "project": current_project, "reason": str(error)})
        raise


def set_status(root: Path, *, task_id: str, requested: str, basis: str | None, artifacts: tuple[str, ...] = (), session: str = "cli", run: str | None = None) -> Transition:
    """Apply a status request under the completion rule (FR-007).

    A completion without proof never reaches Orca: it is recorded as `unverified` and the task keeps its
    previous status. Every other request is sent as given.
    """
    status, reason = orca.completion(requested, basis)
    if status == "unverified":
        events.record(root, guard="orca-completion", kind="unverified", mode="enforce", applied=True, session=session,
                      detail={"task_id": task_id, "requested": requested, "basis": basis, "reason": reason})
        return Transition(decision="unverified", sent=False, reason=reason)
    _call("task-update", "--id", task_id, "--status", status, "--result", json.dumps({"basis": basis, "artifacts": list(artifacts)}, ensure_ascii=False), *_run_args(run))
    return Transition(decision=status, sent=True, reason=reason)


def list_tasks(run: str | None = None) -> list[dict[str, Any]]:
    """Every task of the Run the CLI reports, as records."""
    return tasks_of(_call("task-list", *_run_args(run)))


def dispatch_id_of(answer: Any) -> str:
    """The dispatch id from a worker-start answer (`result.dispatchId`)."""
    result = answer.get("result") if isinstance(answer, dict) else None
    if isinstance(result, dict) and isinstance(result.get("dispatchId"), str):
        return result["dispatchId"]
    raise OrcaError("the worker-start answer has no dispatch id")


def worktree_of(answer: Any) -> str | None:
    """The path of the worktree a worker-start answer created, from its `effects`, or None."""
    result = answer.get("result") if isinstance(answer, dict) else None
    for effect in (result.get("effects") if isinstance(result, dict) else None) or []:
        if isinstance(effect, dict) and effect.get("kind") == "worktree" and isinstance(effect.get("id"), str) and "::" in effect["id"]:
            return effect["id"].split("::", 1)[1]
    return None


def _git_common_dir(root: Path) -> Path | None:
    try:
        run = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return (root / run.stdout.strip()).resolve() if run.returncode == 0 and run.stdout.strip() else None


FOLDER_LEDGER = Path(".coding-agent") / "workers.jsonl"


def workers_ledger(root: Path) -> Path:
    """The file listing worker worktrees.

    It lives in the git common directory, so every worktree of the repository reads the same one. A root outside git
    (a folder project holding several repositories) keeps it under `.coding-agent` instead.
    """
    common = _git_common_dir(root)
    return common / "coding-agent-workers.jsonl" if common is not None else root / FOLDER_LEDGER


def worker_records(root: Path) -> list[dict[str, str]]:
    """The worker worktrees still in place: `{worktree, task_id, dispatch, base, repo}`, in the order they were started.

    `repo` is the repository the worktree belongs to, or an empty string in a record written before it was kept.
    A later `removed` line for a worktree drops its earlier record.
    """
    path = workers_ledger(root)
    if not path.exists():
        return []
    live: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("worktree"), str):
            continue
        if record.get("removed") is True:
            live.pop(record["worktree"], None)
        else:
            live[record["worktree"]] = {"worktree": record["worktree"], "task_id": str(record.get("task_id") or ""), "dispatch": str(record.get("dispatch") or ""),
                                        "base": str(record.get("base") or ""), "repo": str(record.get("repo") or "")}
    return list(live.values())


def worker_worktrees(root: Path) -> set[str]:
    """Resolved paths of the worktrees that coding-agent started workers in and has not removed."""
    return {record["worktree"] for record in worker_records(root)}


def _append(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _ledgers(root: Path, repo: Path | None) -> list[Path]:
    """The ledgers a worker of `repo` is kept in: the coordinator root's, and the repository's own when that is another file."""
    paths = [workers_ledger(root)]
    if repo is not None and _git_common_dir(repo) is not None:
        paths.append(workers_ledger(repo))
    return list(dict.fromkeys(paths))


def repo_of(root: Path, record: dict[str, str]) -> Path:
    """The repository a recorded worker's commits are compared in: `root`, or the worker's own repository when it is a different one.

    A recorded directory that is the main checkout of another repository stays compared in `root`, where git cannot
    vouch for it: nothing else holds its commits.
    """
    repo = Path(record["repo"]) if record.get("repo") else None
    if repo is None or not repo.is_dir() or repo == Path(record["worktree"]) or _git_common_dir(repo) in (None, _git_common_dir(root)):
        return root
    return repo


def mark_removed(root: Path, worktree: str) -> None:
    """Record that a worker worktree was removed, so it is no longer a worker's and no longer a cleanup candidate."""
    repo = next((Path(record["repo"]) for record in worker_records(root) if record["worktree"] == worktree and record["repo"]), None)
    for path in _ledgers(root, repo):
        _append(path, {"worktree": worktree, "removed": True})


def release_worker(dispatch: str) -> None:
    """Release the terminal of one settled worker."""
    _call("worker-release", "--dispatch", dispatch)


def remove_worktree(path: str, *, force: bool = False) -> None:
    """Remove a worktree from Orca and git; Orca deletes its branch too. `force` removes one that still has changes."""
    _call_tool("worktree", "rm", "--worktree", f"path:{path}", *(["--force"] if force else []))


def record_worker(root: Path, *, worktree: str, task_id: str, dispatch: str) -> None:
    """Add a worker worktree to the ledger, with the commit it starts from.

    The start commit is what later tells the worker's own commits apart from the difference between branches.
    A worktree of another repository than `root` (a child repository of a folder project) is recorded twice: in that
    repository's ledger, which is what makes a session in the worktree a worker, and in the root's, which is what the
    coordinator's board and cleanup read.
    """
    resolved = Path(worktree).resolve()
    repo = _repo_path(resolved) if resolved.is_dir() else None
    record = {"worktree": str(resolved), "task_id": task_id, "dispatch": dispatch, "base": _head(resolved) if resolved.is_dir() else None,
              "repo": str(repo) if repo is not None else None}
    for path in _ledgers(root, repo):
        _append(path, record)


def _repo_path(root: Path) -> Path | None:
    """The repository path Orca registers: the main worktree, which holds the git common directory."""
    common = _git_common_dir(root)
    return common.parent if common is not None and common.name == ".git" else None


NAME_LIMIT = 40
DISPLAY_LIMIT = 80


def worktree_name(title: str, task_id: str) -> str:
    """A worktree and branch name read from the task title: lower-case ASCII words joined by `-`, at most 40 characters.

    Vietnamese diacritics are removed (`đ` becomes `d`). A title with no usable characters falls back to the task id.
    Orca appends `-2`, `-3`, ... when the name is taken.
    """
    import re
    import unicodedata

    plain = unicodedata.normalize("NFKD", title.replace("đ", "d").replace("Đ", "D"))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()
    words = [word for word in re.split(r"[^a-z0-9]+", plain) if word]
    name = ""
    for word in words:
        candidate = f"{name}-{word}" if name else word
        if len(candidate) > NAME_LIMIT:
            break
        name = candidate
    return name or (words[0][:NAME_LIMIT] if words else f"task-{task_id.removeprefix('task_')[:12]}")


def _title_of(task_id: str, run: str | None) -> str:
    """The task's title as Orca stores it, or an empty string when the task is not listed."""
    for task in list_tasks(run):
        if task.get("id") == task_id:
            return str(task.get("task_title") or task.get("title") or "")
    return ""


def _head(cwd: Path) -> str | None:
    """The commit `cwd` has checked out, or None outside git."""
    try:
        run = subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.strip() if run.returncode == 0 and run.stdout.strip() else None


def _base_args(root: Path, base: str | None = None) -> list[str]:
    """`--base-branch <ref>`: `base` when given, else the commit `root` has checked out.

    Orca creates a worktree from the repository's default base unless told otherwise, so without this a worker
    starts from a commit that lacks the coordinator's latest work (measured on the installed Orca).
    """
    ref = base or _head(root)
    return ["--base-branch", ref] if ref is not None else []


FOLDER_REFUSAL = "Folder projects cannot create orchestration worktrees"
SCAN_DEPTH = 3
SCAN_LIMIT = 20
SCAN_SKIP = frozenset({"node_modules", "venv", "build", "dist", "target", "__pycache__"})


def child_repos(root: Path, depth: int = SCAN_DEPTH) -> list[str]:
    """Git repositories under `root`, as paths relative to it, at most `depth` levels down and `SCAN_LIMIT` of them.

    Hidden directories and dependency or build directories are not entered, and neither is a repository once found.
    """
    found: list[str] = []
    level = [root]
    for _ in range(depth):
        below: list[Path] = []
        for parent in level:
            try:
                children = sorted(child for child in parent.iterdir() if child.is_dir() and not child.is_symlink())
            except OSError:
                continue
            for child in children:
                if child.name.startswith(".") or child.name in SCAN_SKIP:
                    continue
                if (child / ".git").exists():
                    found.append(child.relative_to(root).as_posix())
                else:
                    below.append(child)
        level = below
    return sorted(found)[:SCAN_LIMIT]


def target_repo(root: Path, repo: str | Path | None) -> Path | None:
    """The repository named by `repo` (relative to `root`, or absolute), or None when none was named and `root` is in git.

    Raises when the named path is not a repository Orca can create a worktree from, and when nothing was named while
    `root` is outside git: a folder project has no repository of its own, so the caller must name one.
    """
    if repo is None:
        if _git_common_dir(root) is not None:
            return None
        found = child_repos(root)
        listing = ("Git repositories found under it: " + ", ".join(found) + ".") if found else "No git repository was found under it."
        raise OrcaError(f"{root} is not a git repository, so a worker worktree needs a target: pass --repo PATH (plan nodes: `repo:`). {listing}")
    path = Path(repo)
    named = _repo_path(path if path.is_absolute() else root / path)
    if named is None:
        raise OrcaError(f"--repo {repo}: not a git repository Orca can create a worktree from")
    return named


def _create_worktree(repo: Path, name: str, base: list[str]) -> str:
    """Create a worktree of `repo` through Orca and return its path. A repository Orca does not know is registered, then the creation is tried once more."""
    create = ("worktree", "create", "--name", name, "--repo", f"path:{repo}", "--no-parent", *base)
    try:
        created = _call_tool(*create)
    except OrcaError as error:
        if "repo_not_found" not in str(error):
            raise
        _call_tool("repo", "add", "--path", str(repo))
        created = _call_tool(*create)
    result = created.get("result") if isinstance(created, dict) else None
    worktree = result.get("worktree") if isinstance(result, dict) else None
    path = worktree.get("path") if isinstance(worktree, dict) else None
    if not isinstance(path, str) or not path:
        raise OrcaError("the worktree create answer has no worktree path")
    return path


def _start_in_new_worktree(root: Path, task_id: str, agent: str, name: str, display: str, run: str | None, *,
                           repo: Path | None = None, base: str | None = None) -> tuple[Any, str | None]:
    """Start a worker in a new worktree. Returns the worker-start answer, and the worktree's path when it was created here.

    `new-child` makes the worktree a child of the calling terminal's worktree. Orca answers `selector_not_found`
    when that terminal's workspace is no longer in its catalog (seen when a folder project became a git repository
    after the terminal opened), and refuses outright when the terminal belongs to a folder project. Then, and whenever
    `repo` names the repository, the worktree is created from the repository path and the worker is started on it.
    The worktree is recorded before the worker starts, so the worker's session already finds itself in the ledger.
    """
    if repo is None:
        try:
            return _call("worker-start", "--task", task_id, "--agent", agent, "--worktree", "new-child", "--name", name, "--display-name", display,
                         *_base_args(root, base), *_run_args(run)), None
        except OrcaError as error:
            if "selector_not_found" not in str(error) and FOLDER_REFUSAL not in str(error):
                raise
        repo = _repo_path(root)
        if repo is None:
            raise OrcaError("worker-start could not place a new worktree, and this directory is not a git repository Orca can create one from")
        base_args = _base_args(root, base)
    else:
        base_args = _base_args(repo, base)
    path = _create_worktree(repo, name, base_args)
    record_worker(root, worktree=path, task_id=task_id, dispatch="")
    return _call("worker-start", "--task", task_id, "--agent", agent, "--worktree", f"path:{path}", *_run_args(run)), path


def worker_start(root: Path, *, task_id: str, agent: str, run: str | None = None, session: str = "cli", title: str | None = None,
                 repo: str | Path | None = None, base: str | None = None) -> str:
    """Start one supervised worker on `task_id` in a new worktree, and return the dispatch id.

    The worktree and its branch are named from the task title, and Orca shows the title on the worker's row.
    Without `title`, the title is read from Orca. The worktree is recorded in the workers ledger, which is how a
    session in it is recognised as a worker. `repo` names the repository the worktree is created in (required when
    `root` is outside git); `base` is the ref it starts from, by default the commit that repository has checked out.
    """
    target = target_repo(root, repo)
    title = title if title is not None else _title_of(task_id, run)
    name = worktree_name(title, task_id)
    answer, created = _start_in_new_worktree(root, task_id, agent, name, (title or name)[:DISPLAY_LIMIT], run, repo=target, base=base)
    dispatch = dispatch_id_of(answer)
    worktree = created or worktree_of(answer) or find_worktree(target or root, name)
    if worktree is not None:
        record_worker(root, worktree=worktree, task_id=task_id, dispatch=dispatch)
    events.record(root, guard="coordinator", kind="delegated", mode="enforce", applied=True, session=session,
                  detail={"task_id": task_id, "agent": agent, "dispatch": dispatch, "worktree": worktree, "name": name,
                          "turn_start": "unobserved" if turn_unobserved(answer) else "observed"})
    if turn_unobserved(answer):
        kick_worker(root, task_id=task_id, dispatch=dispatch, session=session)
    return dispatch


def turn_unobserved(answer: Any) -> bool:
    """True when a worker-start answer says the worker was placed but its first turn was not seen (or the CLI exited non-zero)."""
    result = answer.get("result") if isinstance(answer, dict) else None
    result = result if isinstance(result, dict) else {}
    return (result.get("turnStart") == "unobserved" or result.get("stage") == "turn_start_unobserved"
            or (isinstance(answer, dict) and answer.get(EXIT_KEY) not in (None, 0)))


def find_worktree(root: Path, name: str) -> str | None:
    """The path of the linked worktree on branch `name` (or in a directory named `name`), from `git worktree list`, or None.

    Used when a worker-start answer carries no `effects`: Orca has created the worktree, but did not say where.
    """
    try:
        run = subprocess.run(["git", "-C", str(root), "worktree", "list", "--porcelain"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    by_branch: dict[str, str] = {}
    by_dirname: dict[str, str] = {}
    path: str | None = None
    for line in run.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            by_dirname.setdefault(Path(path).name, path)
        elif line.startswith("branch ") and path is not None:
            by_branch.setdefault(line[len("branch "):].strip().removeprefix("refs/heads/"), path)
    return by_branch.get(name) or by_dirname.get(name)


# --- kicking a worker whose first turn Orca did not see ---------------------------------------------------------------

KICK_WAIT_S = 30.0       # how long to wait for the worker's TUI to show a prompt before sending the kick-off
KICK_POLL_S = 2.0        # pause between screen reads while waiting
KICK_SETTLE_S = 5.0      # pause after a send before checking that the text landed
KICK_ATTEMPTS = 3        # one send plus at most two resends
KICK_MARK = "Ban la worker"
PROMPT_MARKS = ("❯", "›", ">", "$")


def kickoff_text(task_id: str, dispatch: str) -> str:
    """The one-line kick-off sent to a worker at an empty prompt.

    It goes through `terminal send --text`, and `_manual_kick` prints it inside double quotes for a person to paste,
    so it holds no newline, no `>`, and nothing a shell expands inside double quotes (backtick, `$`, `"`, `\\`).
    """
    return (f"Ban la worker Orca cho task {task_id} (dispatch {dispatch}). "
            f"Chay lenh [orca orchestration dispatch-show --task {task_id} --preamble --json] de doc preamble + TASK, roi thuc hien dung TASK. "
            "Heartbeat/worker_done co the bi tu choi vi thieu token: van gui worker_done DUNG MOT LAN cuoi cung, "
            f"va ghi bao cao REPORT-{task_id}.md trong worktree roi commit. Khong dung AskUserQuestion.")


def dispatch_of(task_id: str) -> dict[str, Any]:
    """The dispatch record of a task from `dispatch-show`: `{id, status, assignee_handle}` with both key spellings read.

    `dispatch-show` takes no `--run`; it reads the dispatch by task id alone.
    """
    answer = _call("dispatch-show", "--task", task_id)
    result = answer.get("result") if isinstance(answer, dict) else None
    record = result.get("dispatch") if isinstance(result, dict) else None
    if not isinstance(record, dict):
        raise OrcaError(f"dispatch-show has no dispatch for {task_id}")
    return {
        "id": str(record.get("id") or ""),
        "status": str(record.get("status") or ""),
        "assignee_handle": str(record.get("assignee_handle") or record.get("assigneeHandle") or ""),
    }


def screen_of(handle: str) -> str:
    """What the worker's terminal shows, as one string, or an empty string when it cannot be read."""
    try:
        answer = _call_tool("terminal", "read", "--terminal", handle, "--screen")
    except OrcaError:
        return ""
    result = answer.get("result") if isinstance(answer, dict) else None
    terminal = result.get("terminal") if isinstance(result, dict) else None
    tail = terminal.get("tail") if isinstance(terminal, dict) else None
    return "\n".join(str(line) for line in tail) if isinstance(tail, list) else ""


def _at_prompt(screen: str) -> bool:
    return any(line.strip().startswith(PROMPT_MARKS) for line in screen.splitlines())


def _manual_kick(handle: str, task_id: str, dispatch: str) -> str:
    return f'orca terminal send --terminal {handle} --enter --wait-submit 20 --text "{kickoff_text(task_id, dispatch)}"'


def kick_worker(root: Path, *, task_id: str, dispatch: str, session: str = "cli") -> bool:
    """Send the kick-off to a worker whose first turn Orca did not observe. Returns True when the text was seen on its screen.

    Waits up to `KICK_WAIT_S` for the terminal to show a prompt, sends the kick-off with Enter, and checks the screen
    after `KICK_SETTLE_S`; a screen without the text gets the kick-off again, `KICK_ATTEMPTS` sends in all. Nothing is
    raised: a failed kick is recorded, and the command to run by hand is printed on stderr.
    """
    handle = ""
    attempts = 0
    seen = False
    error: str | None = None
    try:
        handle = dispatch_of(task_id)["assignee_handle"]
        if not handle:
            raise OrcaError(f"dispatch-show names no terminal for {task_id}")
        deadline = time.monotonic() + KICK_WAIT_S
        while not _at_prompt(screen_of(handle)) and time.monotonic() < deadline:
            time.sleep(KICK_POLL_S)
        text = kickoff_text(task_id, dispatch)
        while attempts < KICK_ATTEMPTS and not seen:
            _call_tool("terminal", "send", "--terminal", handle, "--enter", "--wait-submit", "20", "--text", text)
            attempts += 1
            time.sleep(KICK_SETTLE_S)
            seen = KICK_MARK in screen_of(handle)
    except OrcaError as failure:
        error = str(failure)[:300]
    events.record(root, guard="coordinator", kind="kicked", mode="enforce", applied=seen, session=session,
                  detail={"task_id": task_id, "dispatch": dispatch, "terminal": handle, "attempts": attempts, "seen": seen, "error": error})
    if not seen:
        why = error or f"the kick-off was not seen on the worker's screen after {attempts} send(s)"
        print(f"worker-start: worker {task_id} may not have received its task ({why}). Send it by hand:\n  "
              + (_manual_kick(handle, task_id, dispatch) if handle else f"orca orchestration dispatch-show --task {task_id} --json  # then orca terminal send --terminal <assignee_handle> ..."),
              file=sys.stderr)
    return seen


# --- settling a task whose worker_done Orca rejected -----------------------------------------------------------------

SETTLED_WORKER_STATES = frozenset({"succeeded", "failed", "abandoned", "stopped", "released", "completed", "cancelled"})


def worker_state(dispatch: str) -> tuple[str, str]:
    """(worker state, dispatch status) from `worker-show`. Assumption: a dispatch is active until one of them is in `SETTLED_WORKER_STATES`."""
    answer = _call("worker-show", "--dispatch", dispatch)
    result = answer.get("result") if isinstance(answer, dict) else None
    result = result if isinstance(result, dict) else {}
    worker = result.get("worker") if isinstance(result.get("worker"), dict) else {}
    record = result.get("dispatch") if isinstance(result.get("dispatch"), dict) else {}
    return str(worker.get("state") or ""), str(record.get("status") or "")


def dispatch_active(dispatch: str) -> bool:
    state, status = worker_state(dispatch)
    return state not in SETTLED_WORKER_STATES and status not in SETTLED_WORKER_STATES


def settle(root: Path, *, task_id: str, basis: str, artifacts: tuple[str, ...] = (), run: str | None = None, session: str = "cli") -> dict[str, Any]:
    """Complete a task whose worker reported but whose report Orca rejected (no capability token).

    While the supervised dispatch is active, Orca refuses `completed` (`task_not_startable`), so the dispatch is
    abandoned first; an already settled dispatch is left alone. The completion rule still applies: `basis` must be proof.
    """
    dispatch: str | None = None
    abandoned = False
    try:
        dispatch = dispatch_of(task_id)["id"] or None
    except OrcaError:
        dispatch = None
    if dispatch and dispatch_active(dispatch):
        _call("worker-abandon", "--dispatch", dispatch)
        abandoned = True
    outcome = set_status(root, task_id=task_id, requested="completed", basis=basis, artifacts=artifacts, session=session, run=run)
    events.record(root, guard="coordinator", kind="settled", mode="enforce", applied=outcome.sent, session=session,
                  detail={"task_id": task_id, "dispatch": dispatch, "abandoned": abandoned, "decision": outcome.decision, "basis": basis})
    return {"task_id": task_id, "dispatch": dispatch, "abandoned": abandoned, "decision": outcome.decision, "sent": outcome.sent, "reason": outcome.reason}


def worktree_for_task(root: Path, task_id: str, run: str | None = None) -> str | None:
    """The worker worktree of `task_id`: the recorded one, else the one git lists under the name read from the task title."""
    for record in worker_records(root):
        if record["task_id"] == task_id:
            return record["worktree"]
    title = _title_of(task_id, run)
    return find_worktree(root, worktree_name(title, task_id))


def kick_by_hand(root: Path, *, task_id: str, run: str | None = None, session: str = "cli") -> dict[str, Any]:
    """`worker-kick`: record the task's worktree as a worker's (if not yet) and send the kick-off to its terminal."""
    dispatch = dispatch_of(task_id)["id"]
    worktree = worktree_for_task(root, task_id, run)
    recorded = False
    if worktree is not None and Path(worktree).resolve() not in {Path(w) for w in worker_worktrees(root)}:
        record_worker(root, worktree=worktree, task_id=task_id, dispatch=dispatch)
        recorded = True
    seen = kick_worker(root, task_id=task_id, dispatch=dispatch, session=session)
    return {"task_id": task_id, "dispatch": dispatch, "worktree": worktree, "recorded": recorded, "kicked": seen}


def delegate(root: Path, *, title: str, spec: str, t_id: str, agent: str, run: str | None = None, session: str = "cli",
             repo: str | Path | None = None, base: str | None = None) -> tuple[str, str]:
    """Create a task for this repository and start a worker on it. Returns (task id, dispatch id).

    The target repository is resolved first, so a folder project without `repo` fails before any task exists.
    """
    target_repo(root, repo)
    task_id = create_task(root, project=orca.project_name(root), t_id=t_id, spec=spec, title=title, run=run)
    return task_id, worker_start(root, task_id=task_id, agent=agent, run=run, session=session, title=title, repo=repo, base=base)


def _reports(answer: Any) -> list[dict[str, Any]]:
    """Worker reports in a `check` answer: `{task_id, dispatch, outcome, type, subject, body}` per message.

    Orca puts `taskId`, `dispatchId`, and `outcome` in the message `payload`, a JSON string.
    """
    result = answer.get("result") if isinstance(answer, dict) else None
    out: list[dict[str, Any]] = []
    for message in (result.get("messages") if isinstance(result, dict) else None) or []:
        if not isinstance(message, dict) or not str(message.get("type", "")).startswith("worker_"):
            continue
        payload = message.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        payload = payload if isinstance(payload, dict) else {}
        out.append({
            "task_id": payload.get("taskId"),
            "dispatch": payload.get("dispatchId"),
            "outcome": payload.get("outcome"),
            "type": message.get("type"),
            "subject": str(message.get("subject") or ""),
            "body": str(message.get("body") or ""),
        })
    return out


def unread_reports(run: str | None = None) -> list[dict[str, Any]]:
    """Worker reports the coordinator has not acknowledged. Reading them this way does not mark them read."""
    return _reports(_call("check", "--peek", *_run_args(run)))


def ack_reports(run: str | None = None) -> list[dict[str, Any]]:
    """Take delivery of the unread reports and acknowledge them. Returns the reports that were acknowledged."""
    answer = _call("check", *_run_args(run))
    reports = _reports(answer)
    result = answer.get("result") if isinstance(answer, dict) else None
    delivery = (result.get("deliveryId") or result.get("delivery_id")) if isinstance(result, dict) else None
    if isinstance(delivery, str) and delivery:
        _call("check", "--ack", delivery, *_run_args(run))
    return reports

