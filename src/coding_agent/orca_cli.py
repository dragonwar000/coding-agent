"""Adapter to the Orca CLI: create, claim, and transition tasks through `orca orchestration` (FR-006 to FR-008).

Every call goes through `binary()`, which reads `CODING_AGENT_ORCA` and defaults to `orca`, so the
tests run the same code against a fake CLI. Rules come from `orca.py`; this module only performs
them and records the outcome.

Assumption, unverified against a live Orca: `task-create` and `task-update` answer an envelope whose
`result.task.id` is the task id (as the orca-guard hint states), and `task-list` answers either
`result.tasks` or a bare list. `ok: false` in an answer is an error.
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


def _call(*args: str) -> Any:
    """Run `orca orchestration <args> --json` and return the parsed answer."""
    command = [binary(), "orchestration", *args, "--json"]
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise OrcaError(f"cannot run {binary()}: {error}") from error
    if run.returncode != 0:
        raise OrcaError(f"orca {args[0]} exited {run.returncode}: {(run.stderr or run.stdout).strip()[:400]}")
    try:
        answer = json.loads(run.stdout)
    except json.JSONDecodeError as error:
        raise OrcaError(f"orca {args[0]} printed no JSON") from error
    if isinstance(answer, dict) and answer.get("ok") is False:
        raise OrcaError(f"orca {args[0]} answered ok:false: {json.dumps(answer, ensure_ascii=False)[:400]}")
    return answer


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
    return [task for task in tasks if isinstance(task, dict)]


def owner_of(root: Path, task_id: str) -> str | None:
    """The repository that created `task_id`, from the link ledger. The last link wins."""
    path = root / ".coding-agent" / "links.jsonl"
    if not path.exists():
        return None
    owner: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("task_id") == task_id:
            owner = record.get("project")
    return owner


def create_task(root: Path, *, project: str, t_id: str, spec: str, title: str) -> str:
    """Create an Orca task that carries the durable `T-id` and the `project`, then link the two ids (FR-006)."""
    header = f"project: {project}\nt_id: {t_id}\n\n"
    task_id = task_id_of(_call("task-create", "--spec", header + spec, "--task-title", title))
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


def set_status(root: Path, *, task_id: str, requested: str, basis: str | None, artifacts: tuple[str, ...] = (), session: str = "cli") -> Transition:
    """Apply a status request under the completion rule (FR-007).

    A completion without proof never reaches Orca: it is recorded as `unverified` and the task keeps its
    previous status. Every other request is sent as given.
    """
    status, reason = orca.completion(requested, basis)
    if status == "unverified":
        events.record(root, guard="orca-completion", kind="unverified", mode="enforce", applied=True, session=session,
                      detail={"task_id": task_id, "requested": requested, "basis": basis, "reason": reason})
        return Transition(decision="unverified", sent=False, reason=reason)
    _call("task-update", "--id", task_id, "--status", status, "--result", json.dumps({"basis": basis, "artifacts": list(artifacts)}, ensure_ascii=False))
    return Transition(decision=status, sent=True, reason=reason)


def list_tasks() -> list[dict[str, Any]]:
    """Every task the Orca CLI reports, as records."""
    return tasks_of(_call("task-list"))
