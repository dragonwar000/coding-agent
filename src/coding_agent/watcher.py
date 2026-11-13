"""`worker-watch`: tell the coordinator when a worker is dead or stuck, with no model and no heartbeat.

Why (2026-11-13): Orca's preamble told every worker to send a `heartbeat` every 5 minutes. Each one cost the worker a
tool call, and Orca then typed "You have N orchestration messages" into the coordinator's terminal, which started a
full coordinator turn that only read "alive". Workers are now told not to send them (`orca_cli.NO_HEARTBEAT`), and
liveness comes from the worker's terminal instead.

`watch` loops every `WATCH_INTERVAL_S` seconds over every unsettled dispatch of the Run. Each scan is `resume.scan`:
Orca CLI reads only (task list, dispatch, screen, and `terminal show` for a screen that is not working), the same
automatic resume of a worker stopped on a transient API error, and the same reminder to a silent worker. Only what
those cannot fix is a death (`verdict`):

- the terminal is closed, or the agent process left it (`resume.terminal_death`);
- the screen shows an API error a retry does not fix (`PERMANENT_MARKS`, or an unknown `API Error`);
- a transient API error is still on screen after `MAX_RESUMES` resumes, or a resume did not change the screen;
- the worker stays silent for `silent_after_s()` after `MAX_NUDGES` reminders.

A death seen on `DEAD_CONFIRM` scans in a row is reported once (`report_death`): one `escalation` to the Run's mailbox,
subject `worker dead: <task title>`, body with task id, dispatch id, terminal, the reason and the last screen lines
(`BODY_LINES` lines at most). The report is appended to the resumes ledger with `kind: death`, so the same dispatch is
never reported twice, by this loop or a later one. A report Orca refused is tried again on the next scan.

The loop ends when no unsettled dispatch is left that has not been reported. `ensure_running` starts it in the
background (after `delegate`, `plan-next`, and from the board while workers are unsettled) unless one already runs for
the Run: the loop writes `ALIVE_FILE` every scan, and a fresh file means it is alive. `CODING_AGENT_WORKER_WATCH=off`
turns the automatic start off; `worker-watch` still runs by hand.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from coding_agent import events, orca_cli, resume

WATCH_ENV = "CODING_AGENT_WORKER_WATCH"
WATCH_INTERVAL_S = resume.WATCH_INTERVAL_S
DEAD_CONFIRM = 2         # scans in a row that must see a death before it is reported: one odd Orca answer is not a death
SCREEN_LINES = 10        # last screen lines in a report
BODY_LINES = 15          # lines in a report's body at most
LINE_CHARS = 160
ALIVE_FILE = Path(".coding-agent") / "state" / "worker-watch.json"
LOG_FILE = Path(".coding-agent") / "worker-watch.log"
STALE_AFTER_S = 3 * WATCH_INTERVAL_S  # an alive file older than this belongs to a loop that is gone
DEATH = "death"


def verdict(item: resume.Finding, after: float | None = None) -> str | None:
    """Why the worker of `item` is dead or stuck for good, or None while a resume or a reminder may still help.

    `item` is a finding after `resume.scan`, so its `action` says what the resume or the reminder decided. `after` is
    `resume.silent_after_s()`; a silent worker that got every reminder counts once it has been silent that long again.
    """
    if item.state == "dead":
        return item.error or "terminal or agent is gone"
    if item.state == "blocked":
        return f"API error a retry does not fix: {item.error}"
    if item.state == "stalled" and item.action == "limit":
        return f"transient API error still on screen after {resume.MAX_RESUMES} resumes: {item.error}"
    if item.state == "stalled" and item.action == "already-resumed":
        return f"a resume was sent and the screen did not change: {item.error}"
    if item.state == "silent" and item.action == "nudge-limit" and after is not None and item.idle_s >= after:
        return f"silent for {resume._minutes(item.idle_s)} min after {resume.MAX_NUDGES} reminders, no worker_done"
    return None


def _key(item: resume.Finding) -> str:
    return item.dispatch or item.task_id


def reported(root: Path, item: resume.Finding) -> bool:
    """True once a death of this dispatch reached the coordinator."""
    return bool(resume._sent(root, _key(item), DEATH))


def screen_tail(screen: str, limit: int = SCREEN_LINES) -> list[str]:
    """The last `limit` lines of a screen with text, each cut to `LINE_CHARS`. Rules, the empty prompt and Claude Code's
    mode footer (`⏵⏵ bypass permissions on ...`) are left out: they are on every screen and say nothing."""
    lines = [line.replace("\xa0", " ").rstrip() for line in screen.splitlines()]
    kept = [line for line in lines if line.strip() and not resume.RULE_LINE.match(line) and line.strip() != "❯"
            and not line.strip().startswith("⏵⏵")]
    return [line if len(line) <= LINE_CHARS else line[:LINE_CHARS] + "…" for line in kept[-limit:]]


def death_body(item: resume.Finding, reason: str, screen: str) -> str:
    """The report's body: ids, the reason, and the last screen lines, `BODY_LINES` lines at most."""
    head = [f"task: {item.task_id}", f"dispatch: {item.dispatch}", f"terminal: {item.terminal}", f"reason: {reason}"[:LINE_CHARS * 2]]
    tail = screen_tail(screen, BODY_LINES - len(head) - 1)
    return "\n".join(head + ["last screen lines:"] + ([f"  {line}" for line in tail] or ["  (the terminal cannot be read)"]))


def report_death(root: Path, item: resume.Finding, reason: str, run: str | None = None, *, session: str = "worker-watch") -> str:
    """Tell the coordinator once that a worker is dead. Returns `reported`, `already-reported` or `report-failed`."""
    key = _key(item)
    if reported(root, item):
        return "already-reported"
    body = death_body(item, reason, orca_cli.screen_of(item.terminal) if item.terminal else "")
    subject = f"{orca_cli.DEATH_SUBJECT}{item.title or item.task_id}"[:120]
    error: str | None = None
    try:
        orca_cli.escalate(run=run, subject=subject, body=body, task_id=item.task_id, dispatch=item.dispatch)
    except orca_cli.OrcaError as failure:
        error = str(failure)[:300]
    orca_cli._append(root / resume.LEDGER, {"ts": int(time.time()), "kind": DEATH, "task_id": item.task_id, "dispatch": key,
                                            "terminal": item.terminal, "reason": reason, "sent": error is None})
    events.record(root, guard="worker-watch", kind="reported" if error is None else "report-failed", mode="enforce",
                  applied=error is None, session=session,
                  detail={"task_id": item.task_id, "dispatch": item.dispatch, "terminal": item.terminal, "state": item.state,
                          "reason": reason, "error": error})
    return "reported" if error is None else "report-failed"


def _mark_alive(root: Path, run: str | None, pid: int | None = None) -> None:
    path = root / ALIVE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": pid or os.getpid(), "run": orca_cli.run_id(run) or "", "ts": time.time()}), encoding="utf-8")
    temporary.replace(path)


def _clear_alive(root: Path) -> None:
    path = root / ALIVE_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def watch(root: Path, run: str | None = None, *, task_ids: list[str] | None = None, interval_s: float = WATCH_INTERVAL_S,
          scans: int | None = None, confirm: int = DEAD_CONFIRM, sleep: Callable[[float], None] = time.sleep,
          out: Callable[[str], None] | None = None) -> int:
    """Scan every `interval_s` seconds, resume and remind as `resume.scan` does, and report each death once. Returns 0.

    Ends when no unsettled, unreported dispatch is left, or after `scans` scans (for tests). Prints one line when a
    worker's state changes and one per report. An Orca that cannot be read is printed once and the loop goes on.
    """
    say = out or (lambda line: print(line, flush=True))
    suspects: dict[str, int] = {}
    shown: dict[str, str] = {}
    done = 0
    try:
        while scans is None or done < scans:
            if done:
                sleep(interval_s)
            done += 1
            _mark_alive(root, run)
            try:
                found = resume.scan(root, run, task_ids=task_ids, session="worker-watch")
            except orca_cli.OrcaError as error:
                if shown.get("") != str(error):
                    say(f"worker-watch: Orca cannot be read: {str(error)[:200]}")
                shown[""] = str(error)
                continue
            shown.pop("", None)
            after = resume.silent_after_s()
            for item in found:
                key = _key(item)
                mark = f"{item.state}|{item.action}|{item.attempt}" if item.attention else ""
                if shown.get(key, "") != mark:
                    say(resume.describe(item).replace("worker-resume:", "worker-watch:", 1))
                shown[key] = mark
                reason = verdict(item, after)
                if reason is None or reported(root, item):
                    suspects.pop(key, None)
                    continue
                suspects[key] = suspects.get(key, 0) + 1
                if suspects[key] < confirm:
                    continue
                outcome = report_death(root, item, reason, run)
                say(f"worker-watch: {item.task_id} " + ("reported dead to the coordinator" if outcome == "reported"
                                                         else "could not be reported, tried again next scan") + f": {reason}")
            if not [item for item in found if not reported(root, item)]:
                say("worker-watch: no unsettled worker left to watch" + (" (the dead ones were reported)" if found else ""))
                return 0
    finally:
        _clear_alive(root)
    return 0


def running(root: Path, run: str | None = None, now: float | None = None) -> bool:
    """True when a watch loop for this Run wrote its alive file less than `STALE_AFTER_S` ago."""
    try:
        record: Any = json.loads((root / ALIVE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    now = time.time() if now is None else now
    return record.get("run") == (orca_cli.run_id(run) or "") and now - float(record.get("ts") or 0) < STALE_AFTER_S


def enabled() -> bool:
    """False when `CODING_AGENT_WORKER_WATCH` is `off` (or `0`, `false`, `no`); on by default."""
    return os.environ.get(WATCH_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def ensure_running(root: Path, run: str | None = None, *, session: str = "cli") -> str | None:
    """Start `worker-watch` in the background unless one already runs for the Run. Never raises.

    Returns `started`, `running`, or None (turned off, or the start failed; a failure is recorded as an event). The
    process is detached from this one (its own session), so it outlives the hook or command that started it; its
    output goes to `LOG_FILE`.
    """
    if not enabled():
        return None
    try:
        if running(root, run):
            return "running"
        chosen = orca_cli.run_id(run)
        package_parent = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(part for part in (package_parent, env.get("PYTHONPATH", "")) if part)
        log_path = root / LOG_FILE
        log_path.parent.mkdir(parents=True, exist_ok=True)
        detach: dict[str, Any] = ({"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
                                  if os.name == "nt" else {"start_new_session": True})
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "coding_agent.cli", "--root", str(root), "worker-watch",
                                        *(["--run", chosen] if chosen else [])],
                                       cwd=str(root), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **detach)
        _mark_alive(root, run, pid=process.pid)
        events.record(root, guard="worker-watch", kind="started", mode="enforce", applied=True, session=session,
                      detail={"pid": process.pid, "run": chosen})
        return "started"
    except Exception as error:  # noqa: BLE001 - called from hooks and commands that must not fail over the watcher
        try:
            events.record(root, guard="worker-watch", kind="start-failed", mode="enforce", applied=False, session=session,
                          detail={"error": f"{type(error).__name__}: {error}"[:300]})
        except OSError:
            pass
        return None
