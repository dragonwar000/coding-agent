"""A task graph for delegated work: a plan file with dependencies, applied to Orca and dispatched in dependency order.

Plan file (YAML):

    tasks:
      - id: protocol
        title: Wire protocol
        spec: Define the message types ...
      - id: server
        title: Server
        spec: Implement the room actor ...
        deps: [protocol]
        agent: codex        # optional; default is the delegate default

`apply` creates one Orca task per node, in dependency order, passing Orca the ids of the tasks it depends on.
The mapping from plan id to Orca task id is kept in `.coding-agent/plan.json`, so applying twice creates nothing new.
`ready` names the nodes whose dependencies are all `completed` in Orca and that have no worker yet; `dispatch_ready`
starts workers for them. A node whose dependency failed or is blocked is never dispatched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import orca, orca_cli

LEDGER = Path(".coding-agent") / "plan.json"
WAITING = ("pending", "ready")


class PlanError(ValueError):
    """The plan file is malformed; the message names the node and the field."""


@dataclass(frozen=True)
class Node:
    id: str
    title: str
    spec: str
    deps: tuple[str, ...]
    agent: str | None


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{where} must be a non-blank string")
    return value.strip()


def parse(raw: Any) -> list[Node]:
    """Validate a parsed plan document and return its nodes in dependency order (dependencies first)."""
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list) or not tasks:
        raise PlanError("the plan needs a non-empty `tasks` list")
    nodes: dict[str, Node] = {}
    for index, item in enumerate(tasks):
        if not isinstance(item, dict):
            raise PlanError(f"tasks[{index}] must be a mapping")
        node_id = _text(item.get("id"), f"tasks[{index}].id")
        if node_id in nodes:
            raise PlanError(f"duplicate task id {node_id!r}")
        deps = item.get("deps", [])
        if not isinstance(deps, list) or not all(isinstance(dep, str) and dep.strip() for dep in deps):
            raise PlanError(f"tasks[{node_id}].deps must be a list of task ids")
        agent = item.get("agent")
        nodes[node_id] = Node(
            id=node_id,
            title=_text(item.get("title"), f"tasks[{node_id}].title"),
            spec=_text(item.get("spec"), f"tasks[{node_id}].spec"),
            deps=tuple(dict.fromkeys(dep.strip() for dep in deps)),
            agent=_text(agent, f"tasks[{node_id}].agent") if agent is not None else None,
        )
    for node in nodes.values():
        for dep in node.deps:
            if dep == node.id:
                raise PlanError(f"task {node.id!r} depends on itself")
            if dep not in nodes:
                raise PlanError(f"task {node.id!r} depends on unknown task {dep!r}")
    ordered: list[Node] = []
    done: set[str] = set()
    remaining = list(nodes.values())
    while remaining:
        free = [node for node in remaining if all(dep in done for dep in node.deps)]
        if not free:
            raise PlanError("the plan has a dependency cycle among: " + ", ".join(sorted(node.id for node in remaining)))
        for node in free:
            ordered.append(node)
            done.add(node.id)
        remaining = [node for node in remaining if node.id not in done]
    return ordered


def load(path: Path) -> list[Node]:
    """Read and validate a plan file."""
    import yaml

    try:
        return parse(yaml.safe_load(path.read_text(encoding="utf-8")))
    except OSError as error:
        raise PlanError(f"cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise PlanError(f"{path} is not valid YAML: {error}") from error


def ledger(root: Path) -> dict[str, str]:
    """Plan id to Orca task id, for the nodes already created."""
    path = root / LEDGER
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {key: value for key, value in data.items() if isinstance(key, str) and isinstance(value, str)} if isinstance(data, dict) else {}


def _save(root: Path, mapping: dict[str, str]) -> None:
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def apply(root: Path, nodes: list[Node], *, run: str | None = None) -> list[tuple[str, str, bool]]:
    """Create the Orca task of every node that has none yet. Returns (plan id, Orca task id, created) per node."""
    mapping = ledger(root)
    project = orca.project_name(root)
    out: list[tuple[str, str, bool]] = []
    for node in nodes:
        if node.id in mapping:
            out.append((node.id, mapping[node.id], False))
            continue
        task_id = orca_cli.create_task(root, project=project, t_id=f"P-{node.id}", spec=node.spec, title=node.title, run=run,
                                       deps=tuple(mapping[dep] for dep in node.deps))
        mapping[node.id] = task_id
        _save(root, mapping)
        out.append((node.id, task_id, True))
    return out


def states(root: Path, nodes: list[Node], *, run: str | None = None) -> dict[str, str]:
    """Each node's state: `unapplied`, or the status Orca reports (`missing` when Orca no longer lists the task)."""
    mapping = ledger(root)
    statuses = {str(task.get("id")): str(task.get("status")) for task in orca_cli.list_tasks(run)}
    return {node.id: ("unapplied" if node.id not in mapping else statuses.get(mapping[node.id], "missing")) for node in nodes}


def ready(nodes: list[Node], state: dict[str, str]) -> list[Node]:
    """Nodes waiting for a worker whose dependencies are all completed."""
    return [node for node in nodes if state[node.id] in WAITING and all(state[dep] == "completed" for dep in node.deps)]


def dispatch_ready(root: Path, nodes: list[Node], *, default_agent: str, run: str | None = None, limit: int = 2) -> list[tuple[str, str, str]]:
    """Start workers for up to `limit` ready nodes. Returns (plan id, Orca task id, dispatch id) per worker started."""
    mapping = ledger(root)
    started: list[tuple[str, str, str]] = []
    for node in ready(nodes, states(root, nodes, run=run))[:max(limit, 0)]:
        dispatch = orca_cli.worker_start(root, task_id=mapping[node.id], agent=node.agent or default_agent, run=run)
        started.append((node.id, mapping[node.id], dispatch))
    return started


def status_lines(nodes: list[Node], state: dict[str, str]) -> list[str]:
    """One line per node, in dependency order: state, id, title, and the dependencies still open."""
    free = {node.id for node in ready(nodes, state)}
    lines: list[str] = []
    for node in nodes:
        open_deps = [dep for dep in node.deps if state[dep] != "completed"]
        label = "ready" if node.id in free else state[node.id]
        suffix = f" (chờ: {', '.join(open_deps)})" if open_deps else ""
        lines.append(f"[{label}] {node.id}: {node.title}{suffix}")
    return lines
