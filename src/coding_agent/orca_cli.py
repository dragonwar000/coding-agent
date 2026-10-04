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
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import events, orca

BIN_ENV = "CODING_AGENT_ORCA"
RUN_ENV = "CODING_AGENT_ORCA_RUN"
TIMEOUT_S = 60


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


def _run_orca(*words: str) -> Any:
    args = words[1:] if words[0] == "orchestration" else words
    command = [binary(), *words, "--json"]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
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


def workers_ledger(root: Path) -> Path | None:
    """The file listing worker worktrees. It lives in the git common directory, so every worktree of the repository reads the same one."""
    common = _git_common_dir(root)
    return common / "coding-agent-workers.jsonl" if common is not None else None


def worker_worktrees(root: Path) -> set[str]:
    """Resolved paths of the worktrees that coding-agent started workers in."""
    path = workers_ledger(root)
    if path is None or not path.exists():
        return set()
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("worktree"), str):
            found.add(record["worktree"])
    return found


def record_worker(root: Path, *, worktree: str, task_id: str, dispatch: str) -> None:
    """Add a worker worktree to the ledger. Outside git there is no ledger and nothing is recorded."""
    path = workers_ledger(root)
    if path is None:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"worktree": str(Path(worktree).resolve()), "task_id": task_id, "dispatch": dispatch}) + "\n")


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


def _start_in_new_worktree(root: Path, task_id: str, agent: str, name: str, display: str, run: str | None) -> Any:
    """Start a worker in a new worktree.

    `new-child` makes the worktree a child of the calling terminal's worktree. Orca answers `selector_not_found`
    when that terminal's workspace is no longer in its catalog (seen when a folder project became a git repository
    after the terminal opened). Then the worktree is created from the repository path and the worker is started on it.
    """
    try:
        return _call("worker-start", "--task", task_id, "--agent", agent, "--worktree", "new-child", "--name", name, "--display-name", display, *_run_args(run))
    except OrcaError as error:
        if "selector_not_found" not in str(error):
            raise
    repo = _repo_path(root)
    if repo is None:
        raise OrcaError("worker-start could not place a new worktree, and this directory is not a git repository Orca can create one from")
    created = _call_tool("worktree", "create", "--name", name, "--repo", f"path:{repo}", "--no-parent")
    result = created.get("result") if isinstance(created, dict) else None
    worktree = result.get("worktree") if isinstance(result, dict) else None
    path = worktree.get("path") if isinstance(worktree, dict) else None
    if not isinstance(path, str) or not path:
        raise OrcaError("the worktree create answer has no worktree path")
    return _call("worker-start", "--task", task_id, "--agent", agent, "--worktree", f"path:{path}", *_run_args(run))


def worker_start(root: Path, *, task_id: str, agent: str, run: str | None = None, session: str = "cli", title: str | None = None) -> str:
    """Start one supervised worker on `task_id` in a new worktree, and return the dispatch id.

    The worktree and its branch are named from the task title, and Orca shows the title on the worker's row.
    Without `title`, the title is read from Orca. The worktree is recorded in the workers ledger, which is how a
    session in it is recognised as a worker.
    """
    title = title if title is not None else _title_of(task_id, run)
    name = worktree_name(title, task_id)
    answer = _start_in_new_worktree(root, task_id, agent, name, (title or name)[:DISPLAY_LIMIT], run)
    dispatch = dispatch_id_of(answer)
    worktree = worktree_of(answer)
    if worktree is not None:
        record_worker(root, worktree=worktree, task_id=task_id, dispatch=dispatch)
    events.record(root, guard="coordinator", kind="delegated", mode="enforce", applied=True, session=session,
                  detail={"task_id": task_id, "agent": agent, "dispatch": dispatch, "worktree": worktree, "name": name})
    return dispatch


def delegate(root: Path, *, title: str, spec: str, t_id: str, agent: str, run: str | None = None, session: str = "cli") -> tuple[str, str]:
    """Create a task for this repository and start a worker on it. Returns (task id, dispatch id)."""
    task_id = create_task(root, project=orca.project_name(root), t_id=t_id, spec=spec, title=title, run=run)
    return task_id, worker_start(root, task_id=task_id, agent=agent, run=run, session=session, title=title)


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

