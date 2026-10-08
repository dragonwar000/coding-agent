"""Orca task rules: completion needs proof (Task 3), tasks belong to one repo (Task 4), and the
durable `T-id` is linked to Orca's ephemeral `task_id` (FR-006 to FR-008, FR-013).

The harness never calls Orca from here. Callers pass task records in (Orca's JSON export) and
get decisions back, so every rule is testable without a running Orca.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `predicate`: a command proved it. `verifier`: an independent check proved it. `human`: a person did.
# `agentReported`: the worker only said so. Only the first three close a task.
PROOF_BASES = frozenset({"predicate", "verifier", "human"})
ALL_BASES = PROOF_BASES | {"agentReported"}

# The status enum that `orca_guard` accepts. Kept as one set so the guard and the gate agree.
ORCA_STATUSES = frozenset({"pending", "ready", "dispatched", "completed", "failed", "blocked"})


class ClaimRefused(RuntimeError):
    """A session tried to claim a task that belongs to another repository."""


def project_name(root: Path) -> str:
    """The repository's name: the git top-level directory name, or the directory itself outside git."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
        if top.returncode == 0 and top.stdout.strip():
            return Path(top.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return root.resolve().name


def completion(requested: str, basis: str | None) -> tuple[str, str]:
    """The status a task may take, and why.

    A request to finish is only honoured with proof. Without it the task is reported `unverified`
    so the orchestrator sees the gap instead of a false success.
    """
    if requested != "completed":
        return requested, "status is not a completion"
    if basis is None or basis not in ALL_BASES:
        return "unverified", "no basis was recorded"
    if basis in PROOF_BASES:
        return "completed", f"basis {basis}"
    return "unverified", "basis agentReported is not proof"


@dataclass(frozen=True)
class Finding:
    code: str
    task: str
    detail: str


def check_artifacts(task: dict[str, Any], root: Path) -> list[Finding]:
    """`CLAIMED-DONE BUT ABSENT`: a task that claims completion while a declared artifact is missing."""
    if task.get("status") != "completed":
        return []
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    declared = task.get("artifacts") or result.get("artifacts") or []
    missing = [a for a in declared if not (root / a).exists()]
    return [Finding("CLAIMED-DONE BUT ABSENT", str(task.get("id")), f"missing {a}") for a in missing]


def claim_allowed(task: dict[str, Any], current_project: str) -> None:
    """Refuse a claim from another repository. A task without `project` is refused too: the owner is unknown."""
    owner = task.get("project")
    if not owner:
        raise ClaimRefused(f"task {task.get('id')} has no project; claim it from the repository that created it")
    if owner != current_project:
        raise ClaimRefused(f"task {task.get('id')} belongs to {owner}, not {current_project}")


def link(root: Path, *, t_id: str, task_id: str, project: str) -> None:
    """Record the durable-to-ephemeral link in an append-only ledger (FR-006)."""
    path = root / ".coding-agent" / "links.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t_id": t_id, "task_id": task_id, "project": project}, ensure_ascii=False) + "\n")


def basis_of(task: dict[str, Any]) -> str | None:
    """The basis a task recorded: top-level `basis`, else `result.basis` (where `task-update --result` stores it)."""
    if isinstance(task.get("basis"), str):
        return task["basis"]
    result = task.get("result")
    if isinstance(result, dict) and isinstance(result.get("basis"), str):
        return result["basis"]
    return None


def reconcile(tasks: list[dict[str, Any]], *, project: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Group tasks the way the reconcile report reads them (SC-001): running, never dispatched, stuck, unverified.

    Reporting only: nothing here changes a task.
    """
    groups: dict[str, list[dict[str, Any]]] = {"running": [], "never_dispatched": [], "stuck": [], "unverified": [], "other": []}
    for task in tasks:
        if project is not None and task.get("project") != project:
            continue
        status = task.get("status")
        if status == "dispatched":
            groups["running"].append(task)
        elif status in ("ready", "pending") and not task.get("dispatch"):
            groups["never_dispatched"].append(task)
        elif status == "blocked":
            groups["stuck"].append(task)
        elif status == "completed" and basis_of(task) not in PROOF_BASES:
            groups["unverified"].append(task)
        else:
            groups["other"].append(task)
    return groups
