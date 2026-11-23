"""In-process subagents as the coordinator's workers (`coordinator: {dispatch: subagent}`): the setting and the ledger.

Measured on Claude Code 2.1.330 (2026-11-23) with headless sessions in throwaway git projects, a logging hook on every
event, and project-only settings:

- Who made a tool call: `agent_id` is in the PreToolUse and PostToolUse payload of a subagent's call (foreground,
  background, `isolation: worktree`) and never in the main session's. `session_id` and `transcript_path` are the same
  for both, so they cannot tell them apart. `agent_type` cannot either: a main session started with `--agent foo`
  carries `agent_type: foo` and no `agent_id`. The guard therefore reads `agent_id` only.
- Which event ends a subagent: SubagentStop fires for a foreground, background and worktree subagent that finishes.
  It did not fire for a running background subagent stopped with TaskStop (none in the 14 s the session stayed up), nor
  for one whose session got SIGTERM. PostToolUse of Agent cannot close a background record: it fires at launch
  (`status: async_launched`, `agentId`). For a foreground agent it fires at the end (`status: completed`, `agentId`).
- So a record closes on: SubagentStop; PostToolUse of Agent with `status: completed`; PostToolUse of TaskStop, whose
  `tool_input.task_id` is the agent id; PostToolUseFailure of Agent (an unknown agent type fails this way, with no
  SubagentStart). An Agent call another hook denies leaves only its PreToolUse: no start, no result.
- SubagentStart has `agent_id` and no `tool_use_id`; PreToolUse has `tool_use_id` and no `agent_id`. The PostToolUse
  of Agent is what ties them (`tool_use_id` with `agentId`). Until it arrives a start is paired with the nearest earlier
  unpaired assignment of its session, which skips a denied call left behind.
- A session that ended gets SessionEnd (also on SIGTERM), and `--resume` keeps the `session_id`. `CLAUDE_PROJECT_DIR`
  stays the main project inside a worktree subagent while `cwd` is the worktree, so all records land in one ledger.

Not measured: an interactive Esc on a running subagent, `/clear` with a background subagent running, and SIGKILL. A
`clear` or `resume` SessionEnd does not end a record for that reason, and a session gone without SessionEnd is judged
by how long its transcripts have been quiet.

The ledger is append-only (`.coding-agent/subagents.jsonl`) and is folded on read, because hooks run concurrently and a
start can be written before its assignment. A real outcome written after `lost` replaces it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent import orca_cli
from coding_agent import plan as plan_graph
from coding_agent.manifest import load

LEDGER = Path(".coding-agent") / "subagents.jsonl"
# How a generated hook command names its hook (`gen.command_for`).
LEDGER_MARK = "coding_agent.hooks"
MINUTES_ENV = "CODING_AGENT_SUBAGENT_MINUTES"
DEFAULT_MINUTES = 30
SPAWN_TOOLS = frozenset({"Agent", "Task"})
STOP_TOOL = "TaskStop"
TEXT_CHARS = 200
# A start may be written just before its assignment: hooks of one call run concurrently.
START_SKEW_S = 2.0
# An assignment with no start after this long was denied or never ran. Long enough for a user to answer a permission prompt.
NOT_STARTED_S = 600
# A session with no SessionEnd whose transcripts stayed unchanged this long is treated as gone.
LOST_QUIET_S = 3600
LOST_SHOWN_S = 86400
# These SessionEnd reasons do not end the process that runs the background subagents.
SESSION_CONTINUES = frozenset({"clear", "resume"})
# Outcomes the sweep infers. Any outcome the host reports replaces them.
INFERRED = frozenset({"lost", "not-started"})

REFUSAL = (
    "{command}: refused. This project runs with coordinator.dispatch: subagent, so no Orca task, worktree or terminal "
    "is created. Give this work to a subagent with the Agent tool. For a plan node, start the description with "
    "`<node id>:`; `plan-next` lists the ready nodes and their specs."
)


def dispatch_mode(root: Path) -> str:
    """The project's dispatch mode, read from its manifest now (`subagent` unless it says `orca`).

    No manifest, or one that cannot be read, is `orca`: the guard then keeps refusing the Agent tool.
    """
    path = Path(os.path.abspath(root / "integration.yaml"))
    if not path.exists():
        return "orca"
    try:
        return load(path).coordinator.dispatch
    except Exception:  # a broken manifest keeps the stricter behavior; the hooks and gate report it
        return "orca"


def command_mode(root: Path, coordinator_dir: Path) -> str:
    """The dispatch mode a CLI command runs under: the coordinator's manifest decides, else the one of `--root`.

    So a coordinator that says `dispatch: orca` can still delegate into a repository that has no setting of its own.
    """
    has_manifest = (coordinator_dir / "integration.yaml").exists()
    return dispatch_mode(coordinator_dir if has_manifest else root)


def overdue_minutes() -> int | None:
    """Minutes after which an open subagent is flagged; None when `CODING_AGENT_SUBAGENT_MINUTES=off`."""
    raw = os.environ.get(MINUTES_ENV, "").strip().lower()
    if raw == "off":
        return None
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MINUTES


@dataclass
class Subagent:
    id: str
    description: str
    session: str
    ts: float
    subagent_type: str | None = None
    model: str | None = None
    background: bool = False
    isolation: str | None = None
    parent: str | None = None
    transcript: str | None = None
    agent_id: str | None = None
    outcome: str | None = None
    closed_ts: float | None = None
    result: str = ""
    ended: bool = False

    @property
    def is_open(self) -> bool:
        return self.outcome is None


def _short(value: Any) -> str:
    return " ".join(str(value or "").split())[:TEXT_CHARS]


def _append(root: Path, record: dict[str, Any]) -> None:
    """Append one line in a single write, so concurrent hooks cannot interleave."""
    path = root / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def record_event(root: Path, payload: dict[str, Any], now: float | None = None) -> str | None:
    """Write what this hook payload says about a subagent. Returns the record kind, or None when it says nothing."""
    now = time.time() if now is None else now
    event = payload.get("hook_event_name")
    session = str(payload.get("session_id") or "default")
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    use_id = payload.get("tool_use_id")
    record: dict[str, Any] | None = None
    if event == "PreToolUse" and tool in SPAWN_TOOLS and use_id:
        record = {"kind": "assigned", "id": use_id, "description": _short(tool_input.get("description")),
                  "subagent_type": tool_input.get("subagent_type"), "model": tool_input.get("model"),
                  "background": bool(tool_input.get("run_in_background")), "isolation": tool_input.get("isolation"),
                  "parent": payload.get("agent_id"), "transcript": payload.get("transcript_path")}
    elif event == "SubagentStart" and payload.get("agent_id"):
        record = {"kind": "started", "agent_id": payload["agent_id"], "agent_type": payload.get("agent_type")}
    elif event == "SubagentStop" and payload.get("agent_id"):
        record = {"kind": "closed", "agent_id": payload["agent_id"], "outcome": "completed",
                  "result": _short(payload.get("last_assistant_message"))}
    elif event == "PostToolUse" and tool in SPAWN_TOOLS and use_id:
        agent = response.get("agentId")
        if response.get("status") == "completed":
            record = {"kind": "closed", "id": use_id, "agent_id": agent, "outcome": "completed"}
        elif agent:
            # The response, not the input: with `run_in_background` unset the host may still run it in the background.
            record = {"kind": "linked", "id": use_id, "agent_id": agent, "background": response.get("status") == "async_launched"}
    elif event == "PostToolUse" and tool == STOP_TOOL and tool_input.get("task_id"):
        record = {"kind": "closed", "agent_id": tool_input["task_id"], "outcome": "stopped"}
    elif event == "PostToolUseFailure" and tool in SPAWN_TOOLS and use_id:
        record = {"kind": "closed", "id": use_id, "outcome": "failed", "result": _short(payload.get("error"))}
    elif event == "SessionEnd":
        record = {"kind": "session-ended", "reason": payload.get("reason")}
    if record is None:
        return None
    _append(root, {**record, "session": session, "ts": now})
    return str(record["kind"])


def read(root: Path) -> list[dict[str, Any]]:
    """The ledger's records. A torn or foreign line is skipped."""
    try:
        raw = (root / LEDGER).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and isinstance(item.get("ts"), (int, float)):
            records.append(item)
    return records


def _pair_start(entries: dict[str, Subagent], start: dict[str, Any]) -> Subagent | None:
    """The assignment a start belongs to: the nearest earlier unpaired one of its session, of its type when one is."""
    limit = float(start["ts"]) + START_SKEW_S
    free = [entry for entry in entries.values()
            if entry.agent_id is None and entry.session == start.get("session") and entry.ts <= limit]
    typed = [entry for entry in free if entry.subagent_type == start.get("agent_type")]
    return max(typed or free, key=lambda entry: entry.ts, default=None)


def fold(records: list[dict[str, Any]]) -> list[Subagent]:
    """The subagents the records describe, oldest first. The order of concurrent hooks' lines does not matter."""
    entries: dict[str, Subagent] = {}
    for item in records:
        if item.get("kind") == "assigned" and item.get("id"):
            entries[str(item["id"])] = Subagent(
                id=str(item["id"]), description=str(item.get("description") or ""), session=str(item.get("session")),
                ts=float(item["ts"]), subagent_type=item.get("subagent_type"), model=item.get("model"),
                background=bool(item.get("background")), isolation=item.get("isolation"), parent=item.get("parent"),
                transcript=item.get("transcript"))
    # The host's own link first, so the nearest-assignment pairing only handles what it has not linked yet.
    for item in records:
        if item.get("id") in entries and item.get("agent_id") and item.get("kind") in ("linked", "closed"):
            entries[str(item["id"])].agent_id = str(item["agent_id"])
            entries[str(item["id"])].background |= bool(item.get("background"))
    linked = {entry.agent_id for entry in entries.values() if entry.agent_id}
    for item in records:
        agent = item.get("agent_id")
        if item.get("kind") != "started" or not agent or agent in linked:
            continue
        linked.add(agent)
        entry = _pair_start(entries, item)
        if entry is None:
            entry = Subagent(id=str(agent), description="(không có bản ghi giao việc)", session=str(item.get("session")),
                             ts=float(item["ts"]), subagent_type=item.get("agent_type"))
            entries[entry.id] = entry
        entry.agent_id = str(agent)
    by_agent = {entry.agent_id: entry for entry in entries.values() if entry.agent_id}
    for item in records:
        kind = item.get("kind")
        if kind == "closed":
            entry = entries.get(str(item.get("id"))) or by_agent.get(item.get("agent_id"))
            if entry is not None and item.get("outcome") == "not-started" and entry.agent_id:
                continue  # it did start: a permission prompt held it past the limit
            if entry is not None and (entry.outcome is None or entry.outcome in INFERRED):
                entry.outcome, entry.closed_ts = str(item.get("outcome")), float(item["ts"])
                entry.result = str(item.get("result") or entry.result)
            elif entry is not None and not entry.result:
                entry.result = str(item.get("result") or "")
        elif kind == "session-ended" and item.get("reason") not in SESSION_CONTINUES:
            for entry in entries.values():
                if entry.session == item.get("session") and entry.ts <= float(item["ts"]):
                    entry.ended = True
    return sorted(entries.values(), key=lambda entry: entry.ts)


def _last_activity(entry: Subagent) -> float:
    """When the entry's session or subagent last wrote its transcript; the assignment time when neither can be read."""
    if not entry.transcript:
        return entry.ts
    main = Path(entry.transcript)
    paths = [main] + ([main.with_suffix("") / "subagents" / f"agent-{entry.agent_id}.jsonl"] if entry.agent_id else [])
    times = [entry.ts]
    for path in paths:
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(times)


def sweep(root: Path, session: str, source: str | None, now: float | None = None) -> list[Subagent]:
    """At SessionStart, close the open records whose subagent cannot be running any more. Returns them.

    `lost`: its session ended, or this session was resumed without a SessionEnd for the old process, or another session
    has been quiet for `LOST_QUIET_S`. `not-started`: no start came within `NOT_STARTED_S`. A compaction or `/clear`
    fires SessionStart inside a live process, so the session's own records are left alone then.
    """
    now = time.time() if now is None else now
    closed: list[Subagent] = []
    for entry in fold(read(root)):
        if not entry.is_open:
            continue
        if entry.ended:
            outcome = "lost"
        elif entry.agent_id is None and now - entry.ts > NOT_STARTED_S:
            outcome = "not-started"
        elif entry.session == session:
            outcome = "lost" if source == "resume" else None
        else:
            outcome = "lost" if now - _last_activity(entry) > LOST_QUIET_S else None
        if outcome is None:
            continue
        _append(root, {"kind": "closed", "id": entry.id, "outcome": outcome, "session": session, "ts": now})
        entry.outcome, entry.closed_ts = outcome, now
        closed.append(entry)
    return closed


def _label(entry: Subagent) -> str:
    tags = [tag for tag in (entry.subagent_type, entry.model, "nền" if entry.background else None,
                            "worktree" if entry.isolation == "worktree" else None) if tag]
    name = entry.description or entry.id
    return f"{name} [{', '.join(tags)}]" if tags else name


def ledger_installed(root: Path) -> bool:
    """Whether the project's Claude Code settings run the ledger hooks. The dispatch setting works without them."""
    try:
        return f"{LEDGER_MARK} subagent-assign" in (root / ".claude" / "settings.json").read_text(encoding="utf-8")
    except OSError:
        return False


def board_lines(root: Path, now: float | None = None) -> list[str]:
    """The coordinator's board in subagent mode: open subagents with their age, the overdue ones marked, recent lost ones.

    Without the ledger hooks an empty board would read as "nothing is running", so the first line says it cannot know.
    """
    now = time.time() if now is None else now
    entries = fold(read(root))
    running = [entry for entry in entries if entry.is_open and (entry.agent_id or now - entry.ts <= NOT_STARTED_S)]
    lost = [entry for entry in entries if entry.outcome == "lost" and now - float(entry.closed_ts or 0) <= LOST_SHOWN_S]
    limit = overdue_minutes()
    lines: list[str] = []
    if not ledger_installed(root):
        lines.append("sổ subagent chưa bật: .claude/settings.json chưa có hook subagent-*, nên bảng này không biết subagent "
                     "nào đang chạy. Cài lại harness cho project này (xem README, mục dispatch subagent).")
    if running:
        lines.append(f"subagent đang mở ({len(running)}):")
    for entry in running:
        age = int((now - entry.ts) // 60)
        ref = entry.agent_id or "chưa khởi động"
        row = f"  - {_label(entry)}: {age} phút ({ref})"
        if limit is not None and age >= limit:
            stop = f"dừng bằng TaskStop task_id={entry.agent_id}" if entry.agent_id else "dừng nó"
            row += f" QUÁ {limit} PHÚT: đọc transcript và diff để kiểm, hoặc {stop}"
        lines.append(row)
    if lost:
        lines.append(f"subagent mất dấu, phiên của nó đã kết thúc ({len(lost)}):")
        lines += [f"  - {_label(entry)} ({entry.agent_id or entry.id})" for entry in lost]
        lines.append("  việc cần làm: đọc diff của working tree; phần chưa xong thì giao lại cho subagent mới.")
    return lines or ["bảng việc: không có subagent nào đang mở"]


def node_states(root: Path, nodes: list[plan_graph.Node]) -> dict[str, str]:
    """Each plan node's state from the ledger: the latest subagent whose description starts with `<node id>:`.

    `running` while it is open, `completed` when it finished, else `pending`, so a failed, stopped or lost node is
    ready again. The coordinator still checks a result before relying on it; giving the node to a new subagent reopens it.
    """
    entries = fold(read(root))
    states: dict[str, str] = {}
    for node in nodes:
        mine = [entry for entry in entries if entry.description.startswith(f"{node.id}:")]
        latest = mine[-1] if mine else None
        if latest is None:
            states[node.id] = "pending"
        elif latest.is_open:
            states[node.id] = "running"
        else:
            states[node.id] = "completed" if latest.outcome == "completed" else "pending"
    return states


def earlier_orca_workers(root: Path) -> list[dict[str, Any]]:
    """Worker records still in place, of `root` and the repositories it delegated into: Orca work from before the switch."""
    seen: dict[str, dict[str, Any]] = {}
    for owner in orca_cli.all_roots(root):
        for record in orca_cli.worker_records(owner):
            seen.setdefault(record["worktree"], record)
    return list(seen.values())


def graph_states(root: Path, nodes: list[plan_graph.Node], run: str | None = None) -> dict[str, str]:
    """`node_states`, with the Orca task of a node applied before the switch counted, so its work is not given out twice.

    A subagent's record of the node wins. Otherwise the node takes its Orca status: `completed` counts for its
    dependents, a task with no worker yet (`pending`, `ready`) can go to a subagent, and any other status keeps it from
    being ready. A task Orca does not list is free unless a worker record still holds it. When Orca cannot be read the
    node is `orca-unreadable` and not ready, since it may be running or already done.
    """
    states = node_states(root, nodes)
    mapped = plan_graph.ledger(root)
    earlier = [node for node in nodes if node.id in mapped and states[node.id] == "pending"]
    if not earlier:
        return states
    orca_cli.use_stored_run(root)
    if run is None:
        orca_cli.sync_bound_run(root)
    current = orca_cli.run_id(run)
    held = {record["task_id"]: record for record in earlier_orca_workers(root)}
    wanted = {mapped[node.id] for node in earlier}
    runs = list(dict.fromkeys([current, *(held[task]["run"] or current for task in wanted if task in held)]))
    statuses: dict[str, str] = {}
    unread = False
    for name in runs:
        try:
            statuses.update({str(task.get("id")): str(task.get("status")) for task in orca_cli.list_tasks(name)})
        except orca_cli.OrcaError:
            unread = True
    for node in earlier:
        task = mapped[node.id]
        status = statuses.get(task)
        if status is None:
            states[node.id] = "orca-unreadable" if unread else "missing" if task in held else "pending"
        elif status not in plan_graph.WAITING:
            states[node.id] = status
    return states


def plan_lines(root: Path, nodes: list[plan_graph.Node], *, ready_specs: bool, run: str | None = None) -> list[str]:
    """The graph's state; with `ready_specs` (`plan-next`), first the ready nodes with what a subagent needs."""
    states = graph_states(root, nodes, run)
    lines: list[str] = []
    if ready_specs:
        ready = plan_graph.ready(nodes, states)
        lines.append("plan-next: dispatch subagent, không tạo task, worktree hay terminal Orca. Giao mỗi node dưới đây "
                     "cho một subagent bằng tool Agent, với description bắt đầu bằng `<node id>:`."
                     if ready else "plan-next: không có node nào sẵn sàng (đang chạy, chờ phụ thuộc, hoặc đã xong).")
        for node in ready:
            lines.append(f"ready {node.id}: {node.title}" + (f" [{node.model}]" if node.model else ""))
            lines.append(f"  spec: {node.spec}")
            if node.verify:
                lines.append("  verify: " + " ; ".join(node.verify))
    return lines + plan_graph.status_lines(nodes, states)
