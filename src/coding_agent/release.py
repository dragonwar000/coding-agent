"""End the agent session of a finished worker and keep its worktree, branch and files.

Why (2026-11-23): terminals were closed only inside `cleanup.clean`, when a worktree was removed. A worker that finished
but whose branch was not merged kept a live agent session for as long as the worktree stayed, and those sessions
overloaded the machine.

`sweep` ends a session only when every check below holds, each read fresh from Orca. Anything that cannot be read keeps
the worker:

- the task is `completed` or `failed`, and the dispatch Orca holds for it is the one the ledger recorded;
- the coordinator acknowledged the task's report: a report exists in the Run, and none is unread (`inbox --ack`, Orca's
  acknowledgement or the per-task `acked.jsonl`). A task with no report at all has nothing acknowledged and stays;
- the worker has no open question and no unread message in its mailbox;
- Orca observes the exact worker, live, not `working`, and looked for a prompt it waits on and found none (`agentWait`
  present and null), and its screen is `idle`;
- its terminal has written nothing for the grace period (`CODING_AGENT_RELEASE_IDLE_MINUTES`, default 15);
- the terminal is the one recorded for the dispatch: same handle, same worktree, same process incarnation, and neither
  the coordinator's nor the one running this code;
- no descendant of the terminal except the agent itself holds a listening TCP socket (`ps` and `lsof`). Where that
  cannot be checked (no `lsof`, Windows, a terminal Orca reports no process for) the worker stays.

Ending goes through `worker-release`. Orca answers `retained` / `external_terminal` for a worker in an ordinary Orca
terminal and stops nothing, and only then is that single terminal closed. Any other `retained` reason is respected, and
an uncertain answer is never replaced by a close (Orca's documented rule). A session that already exited is only
recorded.

The outcome is kept in `coding-agent-released.jsonl` beside the workers ledger, so a worker is not tried again; a failed
attempt is retried at most `MAX_ATTEMPTS` times. `CODING_AGENT_AUTO_RELEASE=off` turns the step off.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_agent import events, orca_cli, resume

ENV = "CODING_AGENT_AUTO_RELEASE"
IDLE_ENV = "CODING_AGENT_RELEASE_IDLE_MINUTES"
IDLE_MINUTES = 15
LEDGER_NAME = "coding-agent-released.jsonl"
FOLDER_LEDGER = Path(".coding-agent") / "released.jsonl"
SETTLED = ("completed", "failed")
FINAL = ("released", "ended", "retained", "gave-up")
MAX_ATTEMPTS = 3         # failed releases of one dispatch before it is left alone
MAX_CHECKS = 10          # workers read in depth per pass (about five Orca calls each)
MAX_CLOSES = 3           # sessions ended per pass
PROCESS_TIMEOUT_S = 10
SHELLS = frozenset({"login", "sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "pwsh", "nu"})
SESSION_ID = re.compile(r"^[0-9A-Fa-f-]{36}$")


@dataclass(frozen=True)
class Decision:
    """One worker's outcome. `action`: `release`, `ended` (already gone), `wait` (a later pass may release it) or `keep`."""

    task_id: str
    dispatch: str
    worktree: str
    action: str
    reason: str
    terminal: str = ""


@dataclass
class Sweep:
    released: list[Decision] = field(default_factory=list)
    waiting: list[Decision] = field(default_factory=list)
    kept: list[Decision] = field(default_factory=list)


def enabled() -> bool:
    """False when `CODING_AGENT_AUTO_RELEASE` is `off` (or `0`, `false`, `no`); on by default."""
    return os.environ.get(ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def idle_seconds() -> float:
    """The grace period in seconds. A value that is not a positive number means the default."""
    try:
        minutes = float(os.environ.get(IDLE_ENV, "") or IDLE_MINUTES)
    except ValueError:
        minutes = IDLE_MINUTES
    return (minutes if 0 < minutes < float("inf") else IDLE_MINUTES) * 60


def ledger(owner: Path) -> Path:
    path = orca_cli.workers_ledger(owner)
    return owner / FOLDER_LEDGER if path == owner / orca_cli.FOLDER_LEDGER else path.with_name(LEDGER_NAME)


def marks(owner: Path) -> dict[str, dict[str, Any]]:
    """The latest record per dispatch, with `failures`: how many attempts failed."""
    try:
        lines = ledger(owner).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("dispatch"):
            failures = out.get(record["dispatch"], {}).get("failures", 0) + (record.get("state") == "failed")
            out[record["dispatch"]] = {**record, "failures": failures}
    return out


def _mark(owner: Path, item: Decision, state: str, **extra: Any) -> None:
    orca_cli._append(ledger(owner), {"ts": int(time.time()), "dispatch": item.dispatch, "task_id": item.task_id,
                                     "worktree": item.worktree, "terminal": item.terminal, "state": state, **extra})


def _open_records(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Worker records with a real dispatch and a directory, whose session has no final outcome yet, read locally."""
    seen: set[str] = set()
    out: list[tuple[Path, dict[str, Any]]] = []
    for owner in orca_cli.all_roots(root):
        done = marks(owner)
        for record in orca_cli.worker_records(owner):
            if record["worktree"] in seen:
                continue
            seen.add(record["worktree"])
            mark = done.get(record["dispatch"], {})
            if (record["dispatch"] in ("", "adopted") or mark.get("state") in FINAL or mark.get("failures", 0) >= MAX_ATTEMPTS
                    or not Path(record["worktree"]).is_dir()):
                continue
            out.append((owner, record))
    return out


def pending(root: Path) -> bool:
    """True when a worker's session may still be released. Local files only, so a hook can afford it. Never raises."""
    if not enabled():
        return False
    try:
        return bool(_open_records(root))
    except (OSError, ValueError):
        return False


class _Mail:
    """One Run's messages, read once per pass. Raises `OrcaError` when they cannot be read."""

    def __init__(self, root: Path, run: str | None):
        messages, unread, replies = orca_cli.run_mail(run)
        self.reports = orca_cli._reports({"result": {"messages": messages}})
        self.unread = orca_cli.drop_acked(root, unread)
        self.questions = orca_cli.open_questions(messages, replies)

    def reported(self, task_id: str, dispatch: str) -> bool:
        """True when this dispatch reported. A report of an earlier attempt of the task does not count."""
        return any(str(report.get("task_id")) == task_id and report.get("dispatch") in (dispatch, None, "") for report in self.reports)

    def unread_report(self, task_id: str) -> bool:
        return any(str(report.get("task_id")) == task_id for report in self.unread)

    def asking(self, task_id: str, dispatch: str) -> bool:
        return any(str(item.get("task_id")) == task_id or (dispatch and item.get("dispatch") == dispatch) for item in self.questions)


class _Processes:
    """The process table and Orca's terminal roots, read at most once per pass."""

    def __init__(self) -> None:
        self._table: list[tuple[int, int, str]] | None = None
        self._read = False
        self._pids: dict[str, int] | None = None

    def table(self) -> list[tuple[int, int, str]] | None:
        if not self._read:
            self._read = True
            self._table = _process_table()
        return self._table

    def pid(self, pty: str) -> int | None:
        if self._pids is None:
            try:
                self._pids = orca_cli.pty_pids()
            except orca_cli.OrcaError:
                self._pids = {}
        return self._pids.get(pty)

    def tree(self, pty: str) -> list[tuple[int, str]] | None:
        """(pid, program name) of the terminal's root process and its descendants, or None when unknown."""
        pid = self.pid(pty) if pty else None
        table = self.table()
        if pid is None or table is None or not any(row[0] == pid for row in table):
            return None
        children: dict[int, list[tuple[int, str]]] = {}
        for child, parent, name in table:
            children.setdefault(parent, []).append((child, name))
        name = next(row[2] for row in table if row[0] == pid)
        found, todo, seen = [], [(pid, name)], {pid}
        while todo:
            current = todo.pop()
            found.append(current)
            for child in children.get(current[0], []):
                if child[0] not in seen:
                    seen.add(child[0])
                    todo.append(child)
        return found


def _program(command: str) -> str:
    return os.path.basename(command.strip()).lstrip("-").lower()


def _process_table() -> list[tuple[int, int, str]] | None:
    try:
        run = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, errors="replace",
                             timeout=PROCESS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode != 0:
        return None
    table: list[tuple[int, int, str]] = []
    for line in run.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table.append((int(parts[0]), int(parts[1]), _program(parts[2])))
    return table or None


def _listening(pids: list[int]) -> bool | None:
    """True when one of `pids` holds a listening TCP socket, None when `lsof` cannot say.

    `lsof` exits 1 with no output when nothing matches (measured on macOS), so only that exact answer means "none".
    """
    if not pids:
        return False
    try:
        run = subprocess.run(["lsof", "-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-p", ",".join(str(pid) for pid in pids), "-Fp"],
                             capture_output=True, text=True, errors="replace", timeout=PROCESS_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if run.returncode == 0:
        return bool(run.stdout.strip())
    return False if run.returncode == 1 and not run.stdout.strip() and not run.stderr.strip() else None


def _server_running(pty: str, agent: str, processes: _Processes) -> bool | None:
    """True when a descendant other than the agent listens on TCP, None when that cannot be checked."""
    tree = processes.tree(pty)
    if tree is None:
        return None
    return _listening([pid for pid, name in tree if name != _program(agent or "")])


def _protected(dispatch: dict[str, Any]) -> set[str]:
    return {handle for handle in (os.environ.get("ORCA_TERMINAL_HANDLE"), dispatch.get("creator_handle")) if handle}


def _same_process(detail: dict[str, Any], dispatch: dict[str, Any]) -> bool:
    return bool(dispatch.get("process_incarnation")) and dispatch["process_incarnation"] == f"{detail.get('ptyId')}:{detail.get('incarnationId')}"


def _judge(record: dict[str, Any], mail: _Mail, run: str | None, grace: float, now: float,
           processes: _Processes) -> tuple[Decision, dict[str, Any], dict[str, Any]]:
    """The decision for one settled worker, with the dispatch and terminal it was based on."""
    task_id, recorded, worktree = record["task_id"], record["dispatch"], record["worktree"]

    def out(action: str, reason: str, handle: str = "", dispatch: dict[str, Any] | None = None,
            detail: dict[str, Any] | None = None) -> tuple[Decision, dict[str, Any], dict[str, Any]]:
        return Decision(task_id, recorded, worktree, action, reason, handle), dispatch or {}, detail or {}

    dispatch = orca_cli.dispatch_of(task_id)
    if dispatch["id"] != recorded:
        return out("keep", "task has a newer dispatch")
    handle = dispatch["assignee_handle"]
    if not handle:
        return out("keep", "dispatch names no terminal")
    if handle in _protected(dispatch):
        return out("keep", "terminal is the coordinator's", handle)
    detail = orca_cli.terminal_detail(handle)
    if detail is None:
        return out("ended", "terminal already closed", handle)
    observation = orca_cli.worker_observation(recorded)
    if observation["status"] == "exited":
        return out("ended", "agent already exited", handle, detail=detail)
    if not orca_cli._same_dir(str(detail.get("worktreePath") or ""), worktree) or not _same_process(detail, dispatch):
        return out("keep", "terminal is not provably this worker's", handle)
    if not observation["exact"] or observation["status"] != "live":
        return out("keep", f"Orca does not observe this exact worker live ({observation['status'] or 'unknown'})", handle)
    if not mail.reported(task_id, recorded):
        return out("wait", "no report acknowledged yet", handle)
    if mail.unread_report(task_id):
        return out("wait", "report not acknowledged", handle)
    if mail.asking(task_id, recorded):
        return out("wait", "open question", handle)
    if orca_cli.unread_for_terminal(handle, run):
        return out("wait", "unread message for the worker", handle)
    if observation["activity"] == "working":
        return out("wait", "worker is working", handle)
    if not observation["wait_known"] or observation["agent_wait"] is not None:
        return out("wait", "worker may be waiting on a prompt", handle)
    if resume.classify(orca_cli.screen_of(handle)).state != "idle":
        return out("wait", "screen is not idle", handle)
    last = detail.get("lastOutputAt")
    if not isinstance(last, (int, float)) or isinstance(last, bool):
        return out("wait", "last output time unknown", handle)
    if now - last / 1000 < grace:
        return out("wait", "inside the idle grace period", handle)
    server = _server_running(str(detail.get("ptyId") or ""), str(detail.get("agentIdentity") or ""), processes)
    if server is None:
        return out("wait", "listening sockets cannot be checked", handle)
    if server:
        return out("wait", "a process in the terminal is listening on a port", handle)
    return out("release", "idle, acknowledged", handle, dispatch, detail)


def _session_id(root: Path, record: dict[str, Any]) -> str:
    """The worker's Claude Code session id, from the transcript the hooks recorded, or empty when unknown."""
    try:
        known = resume._known_transcripts(root)
    except (OSError, ValueError):
        return ""
    for name in (record["dispatch"], ""):
        path = known.get((record["task_id"], name))
        if path and SESSION_ID.match(Path(path).stem):
            return Path(path).stem
    return ""


def _warn(root: Path, warnings: list[str] | None, session: str, item: Decision, error: str) -> None:
    if warnings is not None:
        warnings.append(f"{Path(item.worktree).name} ({item.task_id}): release failed: {error[:200]}")
    try:
        events.record(root, guard="coordinator", kind="worker-session-release-failed", mode="enforce", applied=False, session=session,
                      detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal, "error": error[:300]})
    except OSError:
        pass


def _end(root: Path, owner: Path, record: dict[str, Any], item: Decision, dispatch: dict[str, Any], detail: dict[str, Any],
         session: str, warnings: list[str] | None) -> str:
    """End one session after reading its terminal once more. Returns `released`, `retained`, `gave-up` or `retry`."""
    agent = str(detail.get("agentIdentity") or "")
    failures = marks(owner).get(item.dispatch, {}).get("failures", 0)

    def failed(error: str) -> str:
        _warn(root, warnings, session, item, error)
        _mark(owner, item, "failed", error=error[:300])
        if failures + 1 < MAX_ATTEMPTS:
            return "retry"
        _mark(owner, item, "gave-up", error=error[:300])
        return "gave-up"

    try:
        fresh = orca_cli.terminal_detail(item.terminal)
        if fresh is None or not _same_process(fresh, dispatch) or fresh.get("lastOutputAt") != detail.get("lastOutputAt"):
            return "retry"  # it changed since the checks: the next pass judges it again
        outcome = orca_cli.release_worker(item.dispatch)
        if outcome.state == "retained" and outcome.reason == "external_terminal":
            orca_cli.close_terminal(item.terminal)
        elif outcome.state == "retained":
            _mark(owner, item, "retained", reason=outcome.reason)
            return "retained"
        elif outcome.state not in ("released", "already_released"):
            return failed(f"worker-release answered {outcome.state or 'nothing'}")
    except orca_cli.OrcaError as error:
        return failed(str(error))
    session_id = _session_id(owner, record)
    _mark(owner, item, "released", agent=agent, session_id=session_id, release=outcome.state)
    events.record(root, guard="coordinator", kind="worker-session-released", mode="enforce", applied=True, session=session,
                  detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal, "worktree": item.worktree,
                          "release": outcome.state, "reason": outcome.reason})
    return "released"


def sweep(root: Path, run: str | None = None, *, session: str = "worker-watch", warnings: list[str] | None = None,
          dry_run: bool = False, now: float | None = None) -> Sweep:
    """End the sessions of finished workers that no longer need one. Never raises for an Orca failure.

    Records come from `root` and every repository it delegated into, each read in its own Run. At most `MAX_CHECKS`
    workers are read in depth and `MAX_CLOSES` ended per pass, rotating so that none is starved. `dry_run` reads and
    decides but ends nothing and writes nothing.
    """
    out = Sweep()
    if not enabled():
        return out
    records = _open_records(root)
    if not records:
        return out
    now = time.time() if now is None else now
    current = orca_cli.run_id(run)
    grace = idle_seconds()
    tasks: dict[str | None, dict[str, dict[str, Any]] | None] = {}
    mails: dict[str | None, _Mail | None] = {}
    processes = _Processes()
    settled: list[tuple[Path, dict[str, Any], str | None]] = []
    for owner, record in records:
        key = record["run"] or current
        if key not in tasks:
            try:
                tasks[key] = {str(task.get("id")): task for task in orca_cli.list_tasks(key)}
            except orca_cli.OrcaError:
                tasks[key] = None
        listed = tasks[key]
        if listed is None:
            out.waiting.append(Decision(record["task_id"], record["dispatch"], record["worktree"], "wait", "Run cannot be read"))
        elif (listed.get(record["task_id"]) or {}).get("status") in SETTLED:
            settled.append((owner, record, key))
    start = int(now // 300) % len(settled) if settled else 0
    closes = 0
    for index, (owner, record, key) in enumerate(settled[start:] + settled[:start]):
        if index >= MAX_CHECKS or closes >= MAX_CLOSES:
            out.waiting.append(Decision(record["task_id"], record["dispatch"], record["worktree"], "wait", "next pass"))
            continue
        try:
            if key not in mails:
                try:
                    mails[key] = _Mail(root, key)
                except orca_cli.OrcaError:
                    mails[key] = None
            mail = mails[key]
            if mail is None:
                out.waiting.append(Decision(record["task_id"], record["dispatch"], record["worktree"], "wait", "messages cannot be read"))
                continue
            item, dispatch, detail = _judge(record, mail, key, grace, now, processes)
        except orca_cli.OrcaError as error:
            out.waiting.append(Decision(record["task_id"], record["dispatch"], record["worktree"], "wait", f"Orca: {str(error)[:120]}"))
            continue
        if item.action == "ended":
            out.released.append(item)
            if not dry_run:
                _mark(owner, item, "ended", agent=str(detail.get("agentIdentity") or ""), session_id=_session_id(owner, record))
        elif item.action == "release":
            result = "released"
            if not dry_run:
                closes += 1
                result = _end(root, owner, record, item, dispatch, detail, session, warnings)
            (out.released if result == "released" else out.waiting if result == "retry" else out.kept).append(item)
        elif item.action == "wait":
            out.waiting.append(item)
        else:
            out.kept.append(item)
    return out


def _resume_hint(worktree: str, mark: dict[str, Any]) -> str:
    agent = str(mark.get("agent") or "claude")
    if agent != "claude":
        return f"mở lại {agent} trong {worktree}"
    session_id = str(mark.get("session_id") or "")
    return f"cd {worktree} && claude --resume{' ' + session_id if session_id else ''}"


def board_lines(root: Path, remaining: list[Any]) -> list[str]:
    """Released workers whose worktree still holds unfinished work, with the command that resumes each. Never raises."""
    rows: list[str] = []
    try:
        known = {record["worktree"]: (owner, record) for owner in reversed(orca_cli.all_roots(root)) for record in orca_cli.worker_records(owner)}
        for item in remaining:
            if item.worktree not in known or not item.exists:
                continue
            owner, record = known[item.worktree]
            mark = marks(owner).get(record["dispatch"], {})
            if mark.get("state") in ("released", "ended"):
                rows.append(f"  - {Path(item.worktree).name} ({item.task_id}): {item.describe()}. Tiếp tục: `{_resume_hint(item.worktree, mark)}`")
    except (OSError, ValueError) as error:
        return [f"phiên worker đã đóng, không đọc được sổ: {str(error)[:120]}"]
    return [f"worker đã đóng phiên, worktree còn việc ({len(rows)}):", *rows[:10]] if rows else []


def _busy_shell(pty: str, processes: _Processes) -> bool:
    """True unless the terminal is known to run nothing but its login shell."""
    tree = processes.tree(pty)
    return tree is None or any(name not in SHELLS for _pid, name in tree)


def _foreign_terminals(worktree: str, handle: str, strict: bool, processes: _Processes) -> list[str]:
    """Terminals in `worktree` that removing it would wrongly end: another agent, the terminal running this code, and,
    when `strict`, a shell running anything. The idle startup shell Orca opens in each worktree is not one."""
    own = os.environ.get("ORCA_TERMINAL_HANDLE")
    found: list[str] = []
    for terminal in orca_cli.terminals():
        other, path = terminal.get("handle"), terminal.get("worktreePath")
        if not (isinstance(other, str) and isinstance(path, str) and path and orca_cli._same_dir(path, worktree)) or other == handle:
            continue
        if other == own or terminal.get("agentIdentity") or (strict and _busy_shell(str(terminal.get("ptyId") or ""), processes)):
            found.append(other)
    return found


def _removal_blocker(root: Path, item: Any, run: str | None, strict: bool, mails: dict[str | None, _Mail | None],
                     processes: _Processes) -> str | None:
    """Why removing this merged, clean worktree would end something still needed, or None.

    Off `strict`, a read that fails does not block: a coordinator is present and the worktree holds no work.
    """
    handle = ""
    if item.dispatch and item.dispatch != "adopted":
        try:
            dispatch = orca_cli.dispatch_of(item.task_id)
        except orca_cli.OrcaRefused:
            dispatch = None
        except orca_cli.OrcaError as error:
            return f"Orca: {str(error)[:120]}" if strict else None
        if dispatch is not None and dispatch["id"] == item.dispatch:
            handle = dispatch["assignee_handle"]
            if handle and handle in _protected(dispatch):
                return "terminal của coordinator ở trong worktree"
    try:
        if handle and orca_cli.terminal_detail(handle) is not None:
            observation = orca_cli.worker_observation(item.dispatch)
            if observation["status"] != "exited":
                key = item.run or run
                if key not in mails:
                    try:
                        mails[key] = _Mail(root, key)
                    except orca_cli.OrcaError:
                        mails[key] = None
                mail = mails[key]
                if mail is None:
                    if strict:
                        return "không đọc được hộp thư"
                elif mail.unread_report(item.task_id):
                    return "báo cáo chưa đọc"
                elif mail.asking(item.task_id, item.dispatch):
                    return "worker đang hỏi"
                if strict and not observation["wait_known"]:
                    return "không biết worker có đang chờ không"
                if observation["agent_wait"] is not None:
                    return "worker đang chờ trả lời"
                if observation["activity"] == "working" or resume.classify(orca_cli.screen_of(handle)).state == "working":
                    return "worker đang chạy"
        if handle or strict:
            foreign = _foreign_terminals(item.worktree, handle, strict, processes)
            if foreign:
                return f"terminal khác trong worktree: {', '.join(foreign[:3])}"
    except orca_cli.OrcaError as error:
        return f"Orca: {str(error)[:120]}" if strict else None
    return None


def removable(root: Path, chosen: list[Any], run: str | None = None, *, strict: bool = False, session: str = "cli",
              warnings: list[str] | None = None) -> list[Any]:
    """The merged, clean worktrees of `chosen` whose removal ends no session that is still needed."""
    mails: dict[str | None, _Mail | None] = {}
    processes = _Processes()
    kept: list[Any] = []
    for item in chosen:
        why = _removal_blocker(root, item, run, strict, mails, processes) if item.exists else None
        if why is None:
            kept.append(item)
            continue
        if warnings is not None:
            warnings.append(f"giữ worktree {Path(item.worktree).name} ({item.task_id}): {why}")
        try:
            events.record(root, guard="coordinator", kind="worktree-kept-for-session", mode="enforce", applied=False, session=session,
                          detail={"task_id": item.task_id, "worktree": item.worktree, "reason": why})
        except OSError:
            pass
    return kept
